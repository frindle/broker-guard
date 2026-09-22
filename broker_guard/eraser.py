"""Build the vendored eraser removal command for a broker.

The command shape here must track the REAL vendored CLI in ``vendor/eraser``
(see ``vendor/eraser/docs/commands.md``). The removal verb is ``send``, which
takes ``--broker <id>`` (repeatable) and an optional ``--profile <id>``; there
is no ``remove`` subcommand and no ``--name`` flag. Identity comes from
eraser's own ``~/.eraser/config.yaml`` profile, never from argv -- which also
keeps the person's name out of the process table.
"""
import re

# Deliberately strict: the broker id lands in argv and (via eraser) in file
# paths, so anything that could be a flag, a path traversal or a shell
# metacharacter is rejected rather than escaped.
_BROKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PROFILE_ID_RE = _BROKER_ID_RE


def _validated_id(value, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a non-empty string")
    normalized = value.strip().lower()
    if not _BROKER_ID_RE.match(normalized):
        raise ValueError(f"invalid {label}: {value!r}")
    return normalized


def build_eraser_cmd(
    broker_id: str,
    profile: dict,
    eraser_bin: str = "eraser",
    dry_run: bool = False,
) -> list[str]:
    """Return the argv list that asks eraser to send an opt-out to *broker_id*.

    *profile* is the broker-guard profile dict. Only its ``eraser_profile``
    key (optional) is used -- an eraser profile *id*, not any PII. The command
    is always a list (never a shell string), so no quoting/injection path
    exists.
    """
    normalized = _validated_id(broker_id, "broker_id")
    if not isinstance(eraser_bin, str) or not eraser_bin.strip():
        raise ValueError("eraser_bin must be a non-empty string")

    cmd = [eraser_bin, "send", "--broker", normalized]
    if dry_run:
        cmd.append("--dry-run")

    if not isinstance(profile, dict):
        raise ValueError("profile must be a dict")
    eraser_profile = profile.get("eraser_profile")
    if eraser_profile is not None:
        if not _PROFILE_ID_RE.match(str(eraser_profile).strip().lower()):
            raise ValueError(f"invalid eraser_profile: {eraser_profile!r}")
        cmd += ["--profile", str(eraser_profile).strip().lower()]
    return cmd


def build_eraser_status_cmd(eraser_bin: str = "eraser", limit: int = 50) -> list[str]:
    """argv for ``eraser status`` -- used to re-verify what was actually sent."""
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive int")
    return [eraser_bin, "status", "--limit", str(limit)]


def _optional_profile_args(profile_id: str | None) -> list[str]:
    if profile_id is None:
        return []
    return ["--profile", _validated_id(profile_id, "profile_id")]


def build_eraser_monitor_cmd(eraser_bin: str = "eraser", profile_id: str | None = None) -> list[str]:
    """argv for ``eraser monitor`` -- IMAP inbox scan for broker replies.

    Per ``vendor/eraser/docs/commands.md`` this command takes NO per-broker
    or per-send arguments; it scans the whole configured inbox in one pass
    and requires the ``inbox:`` (IMAP) section of ``~/.eraser/config.yaml``
    to be set. It is therefore a periodic, whole-account maintenance step,
    not something that can be scoped to a single broker's removal request.
    """
    if not isinstance(eraser_bin, str) or not eraser_bin.strip():
        raise ValueError("eraser_bin must be a non-empty string")
    return [eraser_bin, "monitor"] + _optional_profile_args(profile_id)


def build_eraser_fill_cmd(eraser_bin: str = "eraser", profile_id: str | None = None) -> list[str]:
    """argv for ``eraser fill`` -- browser-automates opt-out forms.

    Per ``vendor/eraser/docs/commands.md`` this command also takes NO
    per-broker argument and no documented ``--dry-run`` flag; it walks
    whatever the ``pipeline:`` config section (browser-automation settings)
    tells it to, in one pass. There is no documented way to target a single
    broker or attach a specific ID-document file through this CLI today, so
    callers cannot use this to fill exactly one photo-id-gated broker on
    demand -- see ``broker_guard.autopilot`` for how that limitation is
    handled (queued as ``needs_document`` instead of auto-filled).
    """
    if not isinstance(eraser_bin, str) or not eraser_bin.strip():
        raise ValueError("eraser_bin must be a non-empty string")
    return [eraser_bin, "fill"] + _optional_profile_args(profile_id)


def parse_eraser_result(stdout: str, returncode: int) -> dict:
    """Interpret an eraser invocation into ``{'success', 'detail'}``.

    A non-zero exit is always a failure; a zero exit with output that names a
    failure is also treated as one, because eraser reports per-broker send
    failures on stdout while still exiting 0 for the batch.
    """
    text = stdout if isinstance(stdout, str) else ""
    lowered = text.lower()
    if returncode != 0:
        return {"success": False, "detail": text.strip()[:2000] or f"exit {returncode}"}
    if "failed" in lowered or "error" in lowered:
        return {"success": False, "detail": text.strip()[:2000]}
    return {"success": True, "detail": text.strip()[:2000]}


def status_after_removal(current: str, result: dict) -> str:
    if current == 'pending' and result.get('success'):
        return 'submitted'
    return current


def needs_reverify(status: str, present_now: bool) -> bool:
    return status == 'confirmed' and bool(present_now)
