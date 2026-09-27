#!/usr/bin/env python3
"""Reference impl for: bg-retire-searxng-for-playwright

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/service.py'
t = p.read_text()

OLD = r'''    searx_search = None
    if cfg.searxng_url:
        try:'''
NEW = r'''    searx_search = None
    if cfg.searxng_url and not cfg.playwright_enabled:
        try:'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
