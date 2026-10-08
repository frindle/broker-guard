"""Redacted ID upload: zero original pixels survive, only the redacted copy is
ever loadable for upload, nothing plaintext touches disk, and the policy
classification of the ID-demanding out-of-scope rows is consistent."""
import base64
import dataclasses
import io
import json
import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image

from broker_guard import autopilot, idredact, optout_forms, optout_submit, webui
from broker_guard.config import Config
from broker_guard.crypto import decrypt_field
from broker_guard.profile import Identity
from conftest import FAKE_EMAIL, FAKE_FIRST, FAKE_LAST

# every pixel of the card is a distinct, non-black colour, so a surviving
# original pixel inside a box is detectable by value
W, H = 40, 30


def _card_png(fmt="PNG") -> bytes:
    img = Image.new("RGB", (W, H))
    img.putdata([(10 + (i % W) * 5, 10 + (i // W) * 7, 200) for i in range(W * H)])
    out = io.BytesIO()
    img.save(out, format=fmt)
    return out.getvalue()


def _pixels(png: bytes):
    return Image.open(io.BytesIO(png)).convert("RGB")


# --- redaction ---------------------------------------------------------------

def test_redacted_boxes_are_solid_black_and_the_rest_is_untouched():
    boxes = [(0.25, 0.2, 0.75, 0.5), (0.0, 0.9, 1.0, 1.0)]
    out = _pixels(idredact.redact(_card_png(), boxes))
    orig = _pixels(_card_png())
    inside = set()
    for b in boxes:
        x0, y0, x1, y1 = idredact._box_to_pixels(b, W, H)
        for x in range(x0, x1):
            for y in range(y0, y1):
                inside.add((x, y))
                assert out.getpixel((x, y)) == (0, 0, 0)
    for x in range(W):
        for y in range(H):
            if (x, y) not in inside:
                assert out.getpixel((x, y)) == orig.getpixel((x, y))
    assert inside  # the boxes covered something


def test_jpeg_input_is_redacted_to_exact_black():
    out = _pixels(idredact.redact(_card_png("JPEG"), [(0.1, 0.1, 0.6, 0.6)]))
    x0, y0, x1, y1 = idredact._box_to_pixels((0.1, 0.1, 0.6, 0.6), W, H)
    assert all(out.getpixel((x, y)) == (0, 0, 0)
               for x in range(x0, x1) for y in range(y0, y1))


def test_metadata_is_not_carried_over():
    img = Image.open(io.BytesIO(_card_png()))
    exif = Image.Exif()
    exif[0x010E] = "SECRET-DESCRIPTION"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    assert b"SECRET-DESCRIPTION" in buf.getvalue()
    out = idredact.redact(buf.getvalue(), [(0, 0, 0.5, 0.5)])
    assert b"SECRET-DESCRIPTION" not in out
    assert not Image.open(io.BytesIO(out)).getexif()


@pytest.mark.parametrize("boxes", [[], None, [(0.5, 0.5, 0.5, 0.9)], [(0, 0, 2, 1)],
                                    [("a", 0, 1, 1)], [(0, 0, 1)]])
def test_unusable_boxes_are_refused_not_silently_ignored(boxes):
    with pytest.raises(idredact.RedactionError):
        idredact.redact(_card_png(), boxes)


def test_non_image_bytes_are_refused():
    with pytest.raises(idredact.RedactionError):
        idredact.redact(b"not an image", [(0, 0, 1, 1)])


def test_guard_bites_a_no_op_redactor_would_fail_the_pixel_test(monkeypatch):
    """Mutation: if the draw step is skipped, original pixels survive and the
    black-box assertion above would fail -- prove the check is able to."""
    from PIL import ImageDraw

    monkeypatch.setattr(ImageDraw.ImageDraw, "rectangle", lambda *a, **k: None)
    out = _pixels(idredact.redact(_card_png(), [(0.25, 0.2, 0.75, 0.5)]))
    x0, y0, _, _ = idredact._box_to_pixels((0.25, 0.2, 0.75, 0.5), W, H)
    assert out.getpixel((x0, y0)) != (0, 0, 0)


# --- storage -----------------------------------------------------------------

@pytest.fixture
def key():
    return Fernet.generate_key().decode("ascii")


def _store_original(directory, side, raw, key):
    from broker_guard.crypto import encrypt_field

    os.makedirs(directory, exist_ok=True)
    tok = encrypt_field(base64.b64encode(raw).decode("ascii"), key.encode("utf-8"))
    with open(os.path.join(directory, side + ".enc"), "w") as fh:
        fh.write(tok)


def test_redacted_copy_is_fernet_encrypted_beside_the_original(tmp_path, key):
    d = str(tmp_path / "ids")
    _store_original(d, "front", _card_png(), key)
    idredact.redact_and_store(d, "front", [(0, 0, 0.5, 0.5)], key)
    path = os.path.join(d, "front.redacted.enc")
    assert os.path.exists(path) and (os.stat(path).st_mode & 0o777) == 0o600
    token = open(path).read()
    assert b"PNG" not in token.encode()
    png = base64.b64decode(decrypt_field(token, key.encode()))
    assert _pixels(png).getpixel((0, 0)) == (0, 0, 0)
    assert idredact.has_redacted(d, "front") and not idredact.has_redacted(d, "back")


def test_cannot_redact_a_side_that_was_never_uploaded(tmp_path, key):
    with pytest.raises(idredact.RedactionError):
        idredact.redact_and_store(str(tmp_path), "front", [(0, 0, 1, 1)], key)


def test_loader_can_only_read_the_redacted_copy_never_the_original(tmp_path, key):
    d = str(tmp_path)
    _store_original(d, "front", _card_png(), key)
    loader = idredact.RedactedIdLoader(d, key)
    assert loader.sides() == () and loader.load("front") is None   # original is invisible to it
    idredact.redact_and_store(d, "front", [(0, 0, 0.5, 0.5)], key)
    assert loader.sides() == ("front",)
    assert _pixels(loader.load("front")).getpixel((0, 0)) == (0, 0, 0)
    assert loader.load("../front") is None


def test_wrong_key_yields_nothing_rather_than_garbage(tmp_path, key):
    d = str(tmp_path)
    _store_original(d, "front", _card_png(), key)
    idredact.redact_and_store(d, "front", [(0, 0, 1, 1)], key)
    other = Fernet.generate_key().decode("ascii")
    assert idredact.RedactedIdLoader(d, other).load("front") is None


# --- the upload step ---------------------------------------------------------

FILE_FIELD = optout_forms.Field(selector="input[type=file][name='file-2']",
                                source="id_front", label="ID upload", kind="file")
FILE_RECIPE = dataclasses.replace(
    optout_forms.CONSUMER_CANVAS,
    fields=tuple(optout_forms.CONSUMER_CANVAS.fields) + (FILE_FIELD,))


@pytest.fixture
def ids(tmp_path, key):
    d = str(tmp_path / "ids")
    _store_original(d, "front", _card_png(), key)
    idredact.redact_and_store(d, "front", [(0, 0, 0.5, 0.5)], key)
    return d


@pytest.fixture
def identity():
    return Identity(first_name=FAKE_FIRST, last_name=FAKE_LAST, middle_name="Q",
                    emails=[FAKE_EMAIL], addresses=["Springfield, IL"])


def _run(tmp_path, identity, loader, page_cls):
    from test_optout_submit import FakeSubmitter, combo_ready

    page = combo_ready()
    page.uploads = []
    page.set_input_files = lambda sel, payload: page.uploads.append((sel, payload))
    cfg = Config(review_dir=str(tmp_path / "review"), optout_submit_enabled=True,
                 optout_submit_dry_run=True)
    rec = optout_submit.submit_optout(
        FILE_RECIPE, identity, cfg, submitter=FakeSubmitter(page), dry_run=True,
        allow_candidate=True, id_loader=loader)
    return page, rec


def test_upload_sends_the_redacted_bytes_from_memory_and_writes_no_file(
        tmp_path, identity, ids, key, monkeypatch):
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    written = []
    real_open = open

    def spy_open(f, mode="r", *a, **k):
        if any(c in mode for c in "wax+") and not str(f).startswith(str(tmp_path / "review")):
            written.append(str(f))
        return real_open(f, mode, *a, **k)

    monkeypatch.setattr("builtins.open", spy_open)
    page, rec = _run(tmp_path, identity, idredact.RedactedIdLoader(ids, key), None)
    monkeypatch.undo()

    assert len(page.uploads) == 1
    sel, payload = page.uploads[0]
    assert sel == FILE_FIELD.selector and isinstance(payload, dict)
    sent = _pixels(payload["buffer"])
    assert sent.getpixel((0, 0)) == (0, 0, 0)                      # redacted region went out black
    assert payload["buffer"] == idredact.RedactedIdLoader(ids, key).load("front")
    assert not [w for w in written if "front" in w or w.endswith(".png")]
    assert os.listdir(scratch) == []                                # no tmp plaintext left
    assert rec["fields"]["ID upload"] == "[redacted ID image]"      # audit record has no image data
    assert "buffer" not in json.dumps(rec)


def test_no_redacted_copy_means_refuse_not_send_the_original(tmp_path, identity, key):
    d = str(tmp_path / "ids")
    _store_original(d, "front", _card_png(), key)               # original only
    page, rec = _run(tmp_path, identity, idredact.RedactedIdLoader(d, key), None)
    assert page.uploads == []
    assert rec["outcome"] == "failed"
    assert "no redacted ID on file" in rec["reason"]


def test_a_file_field_cannot_be_fed_by_a_non_id_source(identity):
    bad = dataclasses.replace(FILE_RECIPE, fields=(
        optout_forms.Field(selector="#f", source="email", label="x", kind="file"),
        optout_forms.Field(selector="#g", source="id_front", label="y", kind="text")))
    with pytest.raises(ValueError):
        optout_forms.resolve_fields(bad, identity, id_sides=("front",))


def test_learned_recipes_can_never_target_an_id_upload():
    from broker_guard import assisted

    assert assisted.keyword_source("Upload a valid government issued photo ID") == assisted.FORBIDDEN
    assert assisted.keyword_source("Upload File", "file-2") == assisted.FORBIDDEN


# --- autopilot ---------------------------------------------------------------

def test_photo_id_auto_sends_once_a_redacted_copy_exists(tmp_path, ids, key):
    cfg = Config(id_documents_dir=ids, crypto_key=key)
    assert autopilot.has_id_documents_on_file(cfg) is True
    assert autopilot.decide_action("photo_id", autopilot.has_id_documents_on_file(cfg))[
        "action"] == "auto_send"
    empty = Config(id_documents_dir=str(tmp_path / "none"), crypto_key=key)
    assert autopilot.decide_action("photo_id", autopilot.has_id_documents_on_file(empty))[
        "action"] == "queue"


# --- classification ----------------------------------------------------------

def test_every_classified_row_is_a_real_out_of_scope_row_with_a_known_demand():
    valid = {"redacted_id", "unredacted", "ssn", "dob", "kba", "selection"}
    for broker_id, demand in optout_forms.OUT_OF_SCOPE_ID_DEMAND.items():
        assert broker_id in optout_forms.OPTOUT_OUT_OF_SCOPE
        assert demand in valid


@pytest.mark.parametrize("broker_id", sorted(
    k for k, v in optout_forms.OUT_OF_SCOPE_ID_DEMAND.items() if v != "redacted_id"))
def test_unredacted_ssn_dob_kba_rows_stay_needs_user_even_with_a_redacted_id(broker_id):
    assert optout_forms.out_of_scope_disposition(broker_id, redacted_id_on_file=True) == "needs_user"


def test_redacted_id_rows_become_automatable_only_with_a_redacted_copy():
    assert optout_forms.out_of_scope_disposition("warmly-ai", False) == "needs_user"
    assert optout_forms.out_of_scope_disposition("warmly-ai", True) == "automatable"
    assert optout_forms.out_of_scope_disposition("not-classified", True) == "needs_user"


# --- web editor --------------------------------------------------------------

@pytest.fixture
def client(tmp_path, profile_file, brokers_file, key):
    cfg = Config(profile_path=profile_file, brokers_path=brokers_file,
                 state_path=str(tmp_path / "s.sqlite"), log_dir=str(tmp_path / "logs"),
                 id_documents_dir=str(tmp_path / "ids"), crypto_key=key,
                 profiles_path=str(tmp_path / "profiles.json"),
                 settings_path=str(tmp_path / "settings.json"))
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    c = TestClient(webui.app)
    c.cfg = cfg
    yield c
    webui.app.dependency_overrides.clear()


def test_editor_flow_upload_then_redact_stores_only_redacted_pixels(client):
    raw = _card_png()
    assert client.post("/identity/id-document", data={"side": "front"},
                       files={"file": ("id.png", raw, "image/png")}).status_code == 200
    page = client.get("/identity/id-document/redact?side=front")
    assert page.status_code == 200 and "no-store" in page.headers["cache-control"]
    r = client.post("/identity/id-document/redact", follow_redirects=False,
                    data={"side": "front", "boxes": json.dumps([[0, 0, 0.5, 0.5]])})
    assert r.status_code == 303
    loader = idredact.loader_from_config(client.cfg)
    assert _pixels(loader.load("front")).getpixel((1, 1)) == (0, 0, 0)
    assert "NOT redacted" not in client.get("/identity").text


def test_editor_rejects_empty_boxes_bad_json_and_missing_upload(client):
    client.post("/identity/id-document", data={"side": "front"},
                files={"file": ("id.png", _card_png(), "image/png")})
    assert client.post("/identity/id-document/redact",
                       data={"side": "front", "boxes": "[]"}).status_code == 400
    assert client.post("/identity/id-document/redact",
                       data={"side": "front", "boxes": "nope"}).status_code == 400
    assert client.post("/identity/id-document/redact",
                       data={"side": "back", "boxes": "[[0,0,1,1]]"}).status_code == 400
    assert client.get("/identity/id-document/redact?side=back").status_code == 404
