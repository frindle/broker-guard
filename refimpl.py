#!/usr/bin/env python3
"""Reference impl for: bg-eraser-cmd

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/eraser.py'
t = p.read_text()

EDITS = [
    # 1. build_eraser_cmd: add config_path + profile_id keywords
    (r'''def build_eraser_cmd(
    broker_id: str,
    profile: dict,
    eraser_bin: str = "eraser",
    dry_run: bool = False,
) -> list[str]:''',
     r'''def build_eraser_cmd(
    broker_id: str,
    profile: dict,
    eraser_bin: str = "eraser",
    dry_run: bool = False,
    config_path: str | None = None,
    profile_id: str | None = None,
) -> list[str]:'''),

    # 2. build_eraser_cmd body: keyword profile_id takes precedence over the
    #    dict's eraser_profile; append --config when given.
    (r'''    if not isinstance(profile, dict):
        raise ValueError("profile must be a dict")
    eraser_profile = profile.get("eraser_profile")
    if eraser_profile is not None:
        if not _PROFILE_ID_RE.match(str(eraser_profile).strip().lower()):
            raise ValueError(f"invalid eraser_profile: {eraser_profile!r}")
        cmd += ["--profile", str(eraser_profile).strip().lower()]
    return cmd''',
     r'''    if not isinstance(profile, dict):
        raise ValueError("profile must be a dict")
    if isinstance(profile_id, str) and profile_id.strip():
        cmd += ["--profile", profile_id]
    else:
        eraser_profile = profile.get("eraser_profile")
        if eraser_profile is not None:
            if not _PROFILE_ID_RE.match(str(eraser_profile).strip().lower()):
                raise ValueError(f"invalid eraser_profile: {eraser_profile!r}")
            cmd += ["--profile", str(eraser_profile).strip().lower()]
    return cmd + _config_args(config_path)'''),

    # 3. build_eraser_status_cmd: add config_path keyword
    (r'''def build_eraser_status_cmd(eraser_bin: str = "eraser", limit: int = 50) -> list[str]:
    """argv for ``eraser status`` -- used to re-verify what was actually sent."""
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive int")
    return [eraser_bin, "status", "--limit", str(limit)]''',
     r'''def build_eraser_status_cmd(eraser_bin: str = "eraser", limit: int = 50, config_path: str | None = None) -> list[str]:
    """argv for ``eraser status`` -- used to re-verify what was actually sent."""
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive int")
    return [eraser_bin, "status", "--limit", str(limit)] + _config_args(config_path)'''),

    # 4. new helper (inserted before _optional_profile_args)
    (r'''def _optional_profile_args(profile_id: str | None) -> list[str]:''',
     r'''def _config_args(config_path: str | None) -> list[str]:
    """Global ``--config <path>`` pair, or nothing when *config_path* is absent.

    eraser's default config location (``$HOME/.eraser/config.yaml``) does not
    exist for the guard user, so callers that know where the real config lives
    pass it here; every builder appends exactly these two argv items and no
    other change to the command shape.
    """
    if isinstance(config_path, str) and config_path.strip():
        return ["--config", config_path]
    return []


def _optional_profile_args(profile_id: str | None) -> list[str]:'''),

    # 5. monitor builder
    (r'''def build_eraser_monitor_cmd(eraser_bin: str = "eraser", profile_id: str | None = None) -> list[str]:''',
     r'''def build_eraser_monitor_cmd(eraser_bin: str = "eraser", profile_id: str | None = None, config_path: str | None = None) -> list[str]:'''),

    (r'''    return [eraser_bin, "monitor"] + _optional_profile_args(profile_id)''',
     r'''    return [eraser_bin, "monitor"] + _optional_profile_args(profile_id) + _config_args(config_path)'''),

    # 6. fill builder
    (r'''def build_eraser_fill_cmd(eraser_bin: str = "eraser", profile_id: str | None = None) -> list[str]:''',
     r'''def build_eraser_fill_cmd(eraser_bin: str = "eraser", profile_id: str | None = None, config_path: str | None = None) -> list[str]:'''),

    (r'''    return [eraser_bin, "fill"] + _optional_profile_args(profile_id)''',
     r'''    return [eraser_bin, "fill"] + _optional_profile_args(profile_id) + _config_args(config_path)'''),
]

for old, new in EDITS:
    assert old in t, f"refimpl anchor not found -- did the target change?\n{old[:80]}..."
    t = t.replace(old, new, 1)

p.write_text(t)
print("refimpl applied")
