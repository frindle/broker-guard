"""Bridge broker_guard's own :class:`profile.Identity` into the vendored
``eraser`` CLI's real config file (``~/.eraser/config.yaml`` by default).

broker-guard never writes this file today, so ``eraser`` has no identity to
send removal requests with. This module provisions/updates it from the
already-loaded ``profile.local.json`` identity, matching the schema read by
``vendor/eraser/internal/config/config.go`` (``Config``/``Profile``/
``Options``) and the fields ``vendor/eraser/cmd/eraser/cmd_init.go`` prompts
for -- see ``vendor/eraser/config.example.yaml`` for the human-facing version
of the same shape.

Field mapping (Identity -> eraser ``profile:`` block)
-------------------------------------------------------
======================  =========================  ================================
Identity field          eraser field                 notes
======================  =========================  ================================
first_name              profile.first_name           required on both sides
middle_name             profile.middle_name           omitted if blank
last_name               profile.last_name             required on both sides
emails[0]                profile.email                 eraser requires an email unless
                                                        options.send_mode: manual
emails[1:]              profile.additional_emails
phones[0]                profile.phone
phones[1:]              profile.additional_phones
addresses[0]             profile.address               Identity has no structured
                                                        city/state/zip -- addresses
                                                        are free-text lines
addresses[1:]            profile.previous_addresses
--                       profile.city / state /         Identity doesn't carry these;
                         zip_code / country /           merge leaves whatever is
                         date_of_birth / name_variants  already in the file untouched
======================  =========================  ================================

Merge, not clobber
-------------------
If ``~/.eraser/config.yaml`` already exists, it is loaded first and only the
``profile:`` sub-keys the Identity actually supplies a non-blank value for
are overwritten -- an existing field is never blanked out by an Identity that
simply doesn't carry that field. Every other top-level block
(``email:``/``options:``/``inbox:``/``pipeline:``/``profiles:``) is carried
over byte-for-byte from the existing file.

If the file does not exist yet, a minimal valid config is written: the
mapped ``profile:`` block plus ``options.send_mode: manual``, since
broker-guard has no SMTP credentials to hand eraser -- this keeps
``Config.Validate()`` happy without an ``email:`` block (manual mode renders
removal emails for the person to send by hand rather than transmitting them).

PII is never logged: only the destination path is ever written to the log.
"""
import logging
import os
import stat

from broker_guard.config import DEFAULT_PROFILE_PATH
from broker_guard.profile import Identity, load_profile

log = logging.getLogger("broker_guard.eraser_config")

DEFAULT_ERASER_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".eraser", "config.yaml"
)


def _profile_block(identity: Identity, existing_profile: dict) -> dict:
    """The eraser ``profile:`` block for *identity*, merged onto whatever
    ``existing_profile`` dict is already on disk (only non-blank Identity
    values are written; every other key of ``existing_profile`` survives)."""
    block = dict(existing_profile or {})

    first_name = identity.first_name.strip()
    last_name = identity.last_name.strip()
    if first_name:
        block["first_name"] = first_name
    if last_name:
        block["last_name"] = last_name
    middle_name = identity.middle_name.strip()
    if middle_name:
        block["middle_name"] = middle_name

    emails = [e.strip() for e in identity.emails if e and e.strip()]
    if emails:
        block["email"] = emails[0]
        if len(emails) > 1:
            block["additional_emails"] = emails[1:]

    phones = [p.strip() for p in identity.phones if p and p.strip()]
    if phones:
        block["phone"] = phones[0]
        if len(phones) > 1:
            block["additional_phones"] = phones[1:]

    addresses = [a.strip() for a in identity.addresses if a and a.strip()]
    if addresses:
        block["address"] = addresses[0]
        if len(addresses) > 1:
            block["previous_addresses"] = addresses[1:]

    return block


def build_eraser_config(identity: Identity, existing: dict | None = None) -> dict:
    """Return the full eraser config dict for *identity*, merged onto
    *existing* (the dict already loaded from ``~/.eraser/config.yaml``, or
    None when provisioning a fresh file)."""
    existing = existing or {}
    cfg = dict(existing)
    cfg["profile"] = _profile_block(identity, existing.get("profile") or {})

    # Carry every other top-level block through untouched.
    for key in ("profiles", "email", "inbox", "pipeline"):
        if key in existing:
            cfg[key] = existing[key]

    options = dict(existing.get("options") or {})
    if not existing:
        # Brand-new file: broker-guard has no SMTP credentials to give
        # eraser, so default to manual send (Config.Validate() then doesn't
        # require an email: block). An existing file's own choice always
        # wins -- this only fires when there was nothing to merge onto.
        options.setdefault("send_mode", "manual")
    cfg["options"] = options

    return cfg


def _load_yaml(path: str) -> dict:
    import yaml

    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a YAML mapping at the top level")
    return data


def _write_yaml(path: str, data: dict) -> None:
    import yaml

    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    # Write then chmod rather than open() with a mode, so this behaves the
    # same on a pre-existing file that had looser permissions.
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, matches config.Save in Go


def provision_eraser_config(identity: Identity, path: str = DEFAULT_ERASER_CONFIG_PATH) -> str:
    """Write/update the eraser config at *path* from *identity*. Returns the
    path written. Never logs identity contents -- only the destination."""
    existing = _load_yaml(path)
    merged = build_eraser_config(identity, existing)
    _write_yaml(path, merged)
    log.info(
        "eraser config %s",
        "updated" if existing else "provisioned",
        extra={"path": path},
    )
    return path


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m broker_guard.eraser_config",
        description="Provision/update ~/.eraser/config.yaml from broker-guard's profile.",
    )
    parser.add_argument(
        "--profile-path",
        default=os.environ.get("BG_PROFILE_PATH", DEFAULT_PROFILE_PATH),
        help="Path to the broker-guard identity JSON (default: BG_PROFILE_PATH env "
             "or profile.local.json).",
    )
    parser.add_argument(
        "--config-path",
        default=DEFAULT_ERASER_CONFIG_PATH,
        help="Path to eraser's config.yaml (default: ~/.eraser/config.yaml).",
    )
    args = parser.parse_args(argv)

    identity = load_profile(args.profile_path)
    path = provision_eraser_config(identity, args.config_path)
    print(f"wrote eraser config to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
