#!/usr/bin/env python3
"""Reference impl for: bg-webui-s2-health-summary

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
p = wt / 'broker_guard/webui_data.py'
t = p.read_text()

if "def load_health_summary" in t:
    print("refimpl already applied -- nothing to do")
    sys.exit(0)

assert "import sqlite3" in t, "refimpl anchor not found -- did the target change?"

# Preserve everything already in the file (query_presence_history and friends);
# only add the json import and append the new function.
t = t.replace("import sqlite3", "import json\nimport sqlite3", 1)

NEWFUNC = '''

def load_health_summary(lines: list[str]) -> dict:
    """Return the most recent health report from ``lines``.

    Each line is one JSON object as written by ``broker_guard/health.py``'s
    ``build_report`` (keys ``total``, ``ok``, ``failed``, ``by_broker``).
    Scans from the end and returns the last line whose ``json.loads`` result
    is a dict; lines that fail to parse or are not JSON objects are skipped.
    An empty list, or one with no valid report, yields the zeroed summary
    ``{"total": 0, "ok": 0, "failed": 0, "by_broker": {}}``.
    """
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return {"total": 0, "ok": 0, "failed": 0, "by_broker": {}}
'''

p.write_text(t.rstrip("\n") + "\n" + NEWFUNC)
print("refimpl applied")
