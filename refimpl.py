#!/usr/bin/env python3
"""Reference impl for: bg-webui-s5-alerts-feed

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

The target already carries earlier slices' work (query_presence_history,
load_health_summary, escalation_countdowns, verify_token) -- this APPENDS
load_recent_alerts and preserves everything else.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/webui_data.py'
t = p.read_text()

if "def load_recent_alerts(" in t:
    print("refimpl already applied -- nothing to do")
    sys.exit(0)

NEW = '''

def load_recent_alerts(lines: list[str], limit: int = 50) -> list[dict]:
    """Return the LAST ``limit`` valid alert digests, newest first.

    Each element of ``lines`` is one JSON object as appended to
    ``logs/alerts.jsonl`` (an alert digest). Blank lines and lines that fail
    to parse are skipped without raising; non-object JSON is not a valid
    entry either. The file's natural append order is oldest-to-newest, so the
    result is the last ``limit`` valid entries in REVERSED (newest-first)
    order. An empty list or an all-invalid one returns ``[]``.
    """
    valid = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            valid.append(entry)
    return list(reversed(valid[-limit:]))
'''

p.write_text(t.rstrip("\n") + "\n" + NEW)
print("refimpl applied")
