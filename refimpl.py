#!/usr/bin/env python3
"""Reference impl for: bg-webui-s4-auth-token

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

The target already carries landed work from earlier slices (query_presence_history,
load_health_summary, escalation_countdowns) -- this ADDS `import hmac` and
verify_token() without touching any of it. Idempotent: re-running is a no-op.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/webui_data.py'
t = p.read_text()

if "def verify_token(" in t:
    print("refimpl already applied -- nothing to do")
    sys.exit(0)

IMPORT_OLD = """import json
import sqlite3
"""
IMPORT_NEW = """import hmac
import json
import sqlite3
"""
assert IMPORT_OLD in t, "refimpl import anchor not found -- did the target change?"
t = t.replace(IMPORT_OLD, IMPORT_NEW, 1)

FUNC = '''

def verify_token(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of a provided token against the expected one.

    ``provided=None`` or an empty string returns ``False`` without raising,
    even when ``expected`` is non-empty; an empty/falsy ``expected`` also
    returns ``False`` (fail closed). The actual comparison goes through
    ``hmac.compare_digest`` so it does not short-circuit on the first
    differing byte.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)
'''
t = t.rstrip("\n") + "\n" + FUNC
p.write_text(t)
print("refimpl applied")
