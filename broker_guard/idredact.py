"""Redacted government-ID copies: render locally, store encrypted, upload only the redacted one.

Policy (Penn, 2026-10-08): broker-guard may send a broker a REDACTED ID. Name
and address stay visible; the document number, photo, date of birth and
barcode are blacked out. The ORIGINAL is never uploaded anywhere -- the only
loader the submit path gets (``RedactedIdLoader``) cannot read it.

Everything here is local: Pillow renders the black boxes, Fernet encrypts the
result with ``BG_CRYPTO_KEY``. Nothing is logged but a side and a byte count.
"""
from __future__ import annotations

import base64
import io
import logging
import os

from broker_guard.crypto import decrypt_field, encrypt_field

log = logging.getLogger(__name__)

SIDES = ("front", "back")
MAX_PIXELS = 40_000_000          # refuse decompression-bomb sized uploads


class RedactionError(ValueError):
    """The image or the boxes cannot produce a trustworthy redaction."""


def original_path(directory: str, side: str) -> str:
    return os.path.join(directory, "{}.enc".format(side))


def redacted_path(directory: str, side: str) -> str:
    return os.path.join(directory, "{}.redacted.enc".format(side))


def _box_to_pixels(box, width: int, height: int) -> tuple:
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        raise RedactionError("a redaction box must be four numbers")
    if not all(0.0 <= v <= 1.0 for v in (x0, y0, x1, y1)):
        raise RedactionError("redaction boxes are fractions of the image (0..1)")
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    # Round OUTWARDS so a box never leaves a one-pixel sliver of the field.
    import math
    px = (max(0, math.floor(x0 * width)), max(0, math.floor(y0 * height)),
          min(width, math.ceil(x1 * width)), min(height, math.ceil(y1 * height)))
    if px[2] - px[0] < 1 or px[3] - px[1] < 1:
        raise RedactionError("a redaction box has no area")
    return px


def redact(image_bytes: bytes, boxes) -> bytes:
    """Return PNG bytes of *image_bytes* with every box painted solid black.

    The output is a NEW image built from pixel data only: EXIF/GPS and every
    other metadata block of the original is dropped, and PNG is lossless so a
    black box is exactly black (JPEG would leave ringing in the boxes).
    At least one box is required -- "redacted" with nothing blacked out would
    be the original under a reassuring name.
    """
    from PIL import Image, ImageDraw, ImageOps

    boxes = list(boxes or [])
    if not boxes:
        raise RedactionError("draw at least one box: nothing would be redacted")
    try:
        src = Image.open(io.BytesIO(image_bytes))
        src.load()
    except Exception as exc:
        raise RedactionError("not a readable image ({})".format(type(exc).__name__))
    if src.width * src.height > MAX_PIXELS:
        raise RedactionError("image is too large")
    src = ImageOps.exif_transpose(src)        # boxes are drawn on the upright view
    rgb = src.convert("RGB")
    pixel_boxes = [_box_to_pixels(b, rgb.width, rgb.height) for b in boxes]
    clean = rgb.copy()
    clean.info.clear()                        # no EXIF / ICC / text chunks carried over
    draw = ImageDraw.Draw(clean)
    for x0, y0, x1, y1 in pixel_boxes:
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=(0, 0, 0))
    out = io.BytesIO()
    clean.save(out, format="PNG")
    return out.getvalue()


def _write_encrypted(path: str, data: bytes, key: str) -> None:
    token = encrypt_field(base64.b64encode(data).decode("ascii"), key.encode("utf-8"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_encrypted(path: str, key: str) -> bytes | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return None
    try:
        return base64.b64decode(decrypt_field(token, key.encode("utf-8")))
    except Exception:
        return None


def load_original(directory: str, side: str, key: str) -> bytes | None:
    """The uploaded original -- for the local redaction editor ONLY."""
    return _read_encrypted(original_path(directory, side), key)


def save_redacted(directory: str, side: str, png_bytes: bytes, key: str) -> None:
    if side not in SIDES:
        raise RedactionError("side must be front or back")
    _write_encrypted(redacted_path(directory, side), png_bytes, key)
    log.info("redacted id stored", extra={"side": side, "bytes": len(png_bytes)})


def redact_and_store(directory: str, side: str, boxes, key: str) -> int:
    """Redact the stored original of *side* and store the result encrypted."""
    original = load_original(directory, side, key)
    if original is None:
        raise RedactionError("no uploaded {} image to redact".format(side))
    out = redact(original, boxes)
    save_redacted(directory, side, out, key)
    return len(out)


def has_redacted(directory: str, side: str = "front") -> bool:
    return os.path.exists(redacted_path(directory, side))


def redacted_sides(directory: str) -> tuple:
    return tuple(s for s in SIDES if has_redacted(directory, s))


class RedactedIdLoader:
    """What the submit path is handed: it can read redacted copies, nothing else."""

    def __init__(self, directory: str, key: str):
        self._dir, self._key = directory, key

    def sides(self) -> tuple:
        return redacted_sides(self._dir)

    def load(self, side: str) -> bytes | None:
        if side not in SIDES:
            return None
        return _read_encrypted(redacted_path(self._dir, side), self._key)


def loader_from_config(cfg):
    key = getattr(cfg, "crypto_key", None)
    directory = getattr(cfg, "id_documents_dir", None)
    if not key or not directory:
        return None
    return RedactedIdLoader(directory, key)


def upload_payload(png_bytes: bytes, side: str) -> dict:
    """Playwright ``set_input_files`` payload: bytes in memory, never a file.

    A path-based upload would need the decrypted ID on disk until the form is
    submitted (Chromium reads the file at submit time, so deleting it after the
    step breaks the upload). An in-memory buffer has nothing to clean up.
    """
    return {"name": "id-{}.png".format(side), "mimeType": "image/png", "buffer": png_bytes}
