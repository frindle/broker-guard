#!/usr/bin/env python3
"""Add and remove prose entries in the broker mapping dicts, idempotently.

The mapping workstream files findings as long prose strings keyed by broker
id, spread across nine dicts in two modules (``search_forms.RECIPES``,
``NO_SEARCH_SURFACE``, ``SEARCH_UNDECIDED``, ``SEARCH_BLOCKED``, and
``optout_forms``' ``NO_OPTOUT_SURFACE``, ``OPTOUT_UNDECIDED``,
``OPTOUT_BLOCKED``, ``OPTOUT_OUT_OF_SCOPE``, ``STAGED_RECIPES``). Doing that by
hand is where the mistakes live, in three specific ways this script removes:

1. DOUBLE-APPLICATION. Research arrives out of order and sometimes twice.
   ``add`` is a no-op when the key is already present, so re-running a batch
   cannot silently append a second entry for the same broker.
2. A BROKER IN TWO STATES AT ONCE. When a verdict is corrected -- an
   "undecided" that rendering proves is "blocked" -- the old entry has to be
   REMOVED, not merely superseded, or ``gen_broker_checklist.py`` reports
   whichever dict it happens to read first. ``drop`` exists for that, and is
   likewise a no-op if the key is already gone.
3. HAND-WRAPPED PROSE. Entries are implicit-concatenated string literals
   wrapped to the repo's width. Doing it by eye produces ragged diffs;
   ``textwrap`` produces the same shape every time.

Neither operation can corrupt an unrelated entry: ``drop`` matches one key's
block anchored at its four-space indent, and ``add`` inserts immediately
before the dict's closing brace.

USAGE
-----
    python3 tools/edit_broker_entries.py ops.json

``ops.json`` maps a module filename to a list of operations::

    {"optout_forms.py": [
       {"op": "drop", "dict": "OPTOUT_UNDECIDED", "key": "example-com"},
       {"op": "add",  "dict": "OPTOUT_BLOCKED",   "key": "example-com",
        "text": "Verified by browser render 2026-09-23: ..."}
     ]}

Each operation prints OK or skip, so a re-run visibly reports that it changed
nothing. ALWAYS follow a run with an import check and the test suite::

    python3 -c "from broker_guard import search_forms, optout_forms"
    python -m pytest tests -q

Straight double quotes in ``text`` are converted to single quotes, since the
generated literals are double-quoted. Write prose accordingly.
"""
import json
import os
import re
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "broker_guard")

WIDTH = 64  # matches the wrapping of the entries already in the modules


def dict_span(src, name):
    """Return (decl_start, body_start, body_end) for a module-level dict.

    ``body_end`` is the index of the newline before the closing brace, which
    is the only place an entry may be inserted.
    """
    m = re.search(r"^%s(?::\s*dict)?\s*=\s*\{" % re.escape(name), src, re.M)
    if not m:
        raise SystemExit("dict not found: %s" % name)
    start = m.end()
    try:
        end = src.index("\n}\n", start)
    except ValueError:
        raise SystemExit("unterminated dict: %s" % name)
    return m.start(), start, end


def drop(src, name, key):
    """Remove one ``"key": ( ... ),`` block. No-op if absent."""
    _, start, end = dict_span(src, name)
    body = src[start:end]
    # Anchored on the four-space indent so a key name appearing inside
    # another entry's prose cannot be mistaken for an entry of its own.
    pat = re.compile(r'\n    "%s": \(.*?\n    \),' % re.escape(key), re.S)
    if not pat.search(body):
        return src, False
    return src[:start] + pat.sub("", body, count=1) + src[end:], True


def render(key, text):
    """Format one entry as wrapped, implicit-concatenated string literals."""
    lines = textwrap.wrap(text, width=WIDTH)
    out = ['    "%s": (' % key]
    for line in lines:
        out.append('        "%s "' % line.replace('"', "'"))
    # The last literal carries no trailing space before the closing paren.
    out[-1] = out[-1].rstrip()[:-2] + '"'
    out.append("    ),")
    return "\n".join(out) + "\n"


def add(src, name, key, text):
    """Insert an entry before the dict's closing brace. No-op if present."""
    _, start, end = dict_span(src, name)
    if re.search(r'\n    "%s": ' % re.escape(key), src[start:end]):
        return src, False
    return src[:end + 1] + render(key, text) + src[end + 1:], True


def apply_ops(ops):
    """Apply {filename: [op, ...]} and return a list of report lines."""
    report = []
    for fname, todo in ops.items():
        path = os.path.join(PKG, fname)
        src = open(path).read()
        for op in todo:
            kind = op["op"]
            if kind == "drop":
                src, did = drop(src, op["dict"], op["key"])
            elif kind == "add":
                src, did = add(src, op["dict"], op["key"], op["text"])
            else:
                raise SystemExit("unknown op: %r" % kind)
            report.append("%s %-5s %-22s %s" % (
                "OK  " if did else "skip", kind, op["dict"], op["key"]))
        open(path, "w").write(src)
    return report


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip().split("USAGE")[-1], file=sys.stderr)
        return 2
    print("\n".join(apply_ops(json.load(open(argv[1])))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
