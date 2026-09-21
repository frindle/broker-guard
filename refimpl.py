#!/usr/bin/env python3
"""Reference impl for: bg-dashboard-s2-identity-view-and-update

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Appends the /identity GET+POST routes to broker_guard/webui.py, preserving the
existing index route. Idempotent: if PROFILE_PATH is already present it is a
no-op.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/webui.py'
t = p.read_text()

if "PROFILE_PATH" in t:
    print("refimpl already applied -- no-op")
    sys.exit(0)

NEW = '''

import json as _json
from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse

import broker_guard.profile as profile

PROFILE_PATH = 'profile.json'


def _split_list(text: str) -> list[str]:
    return [part.strip() for part in (text or '').splitlines() if part.strip()]


@app.get("/identity", response_class=HTMLResponse)
def identity_view():
    try:
        ident = profile.load_profile(PROFILE_PATH)
    except Exception as e:
        raise HTTPException(status_code=422, detail='invalid profile: {}'.format(e))

    def esc(value):
        return (str(value).replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))

    emails = '\\n'.join(ident.emails)
    phones = '\\n'.join(ident.phones)
    addresses = '\\n'.join(ident.addresses)
    return (
        '<!DOCTYPE html>\\n'
        '<html><head><title>Broker Guard -- Identity</title></head><body>\\n'
        '<h1>Identity</h1>\\n'
        '<form method="post" action="/identity">\\n'
        '<label>First name <input name="first_name" value="' + esc(ident.first_name) + '"></label>\\n'
        '<label>Middle name <input name="middle_name" value="' + esc(ident.middle_name) + '"></label>\\n'
        '<label>Last name <input name="last_name" value="' + esc(ident.last_name) + '"></label>\\n'
        '<label>Emails <textarea name="emails">\\n' + emails + '\\n</textarea></label>\\n'
        '<label>Phones <textarea name="phones">\\n' + phones + '\\n</textarea></label>\\n'
        '<label>Addresses <textarea name="addresses">\\n' + addresses + '\\n</textarea></label>\\n'
        '<button type="submit">Save identity</button>\\n'
        '</form>\\n'
        '</body></html>'
    )


@app.post("/identity")
def identity_update(
    first_name: str = Form(""),
    middle_name: str = Form(""),
    last_name: str = Form(""),
    emails: str = Form(""),
    phones: str = Form(""),
    addresses: str = Form(""),
):
    if not first_name.strip() or not last_name.strip():
        raise HTTPException(status_code=422, detail='first_name and last_name are required non-blank strings')

    data = {
        'first_name': first_name,
        'middle_name': middle_name,
        'last_name': last_name,
        'emails': _split_list(emails),
        'phones': _split_list(phones),
        'addresses': _split_list(addresses),
    }

    with open(PROFILE_PATH, 'w', encoding='utf-8') as f:
        _json.dump(data, f, indent=2)
    return RedirectResponse(url="/identity", status_code=303)
'''

p.write_text(t.rstrip() + '\n' + NEW.lstrip('\n'))
print('refimpl applied')
