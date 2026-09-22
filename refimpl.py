#!/usr/bin/env python3
"""Reference impl for: bg-dashboard-s3-brokers-matched-removed

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Adds to broker_guard/webui.py (preserving everything already there):
  * `import broker_guard.state`
  * module-level PRESENCE_DB_PATH constant
  * GET /brokers route: opens the db via state.init_db(PRESENCE_DB_PATH),
    calls webui_data.query_presence_history(conn), renders one row per record
    with identity_key/broker_id/first_seen/last_seen. Zero rows -> empty table,
    no crash.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/webui.py'
t = p.read_text()

OLD_IMPORT = "import broker_guard.profile\n"
NEW_IMPORT = (
    "import broker_guard.profile\n"
    "import broker_guard.state\n"
)

ROUTE = '''PRESENCE_DB_PATH = "data/presence.sqlite3"


@app.get("/brokers", response_class=HTMLResponse)
def brokers():
    conn = broker_guard.state.init_db(PRESENCE_DB_PATH)
    try:
        rows = webui_data.query_presence_history(conn)
    finally:
        conn.close()

    lines = []
    lines.append("<!DOCTYPE html>")
    lines.append("<html><head><title>Broker Guard -- Brokers</title></head><body>")
    lines.append("<h1>Brokers</h1>")
    lines.append('<table border="1">')
    lines.append("<tr><th>identity_key</th><th>broker_id</th>"
                 "<th>first_seen</th><th>last_seen</th></tr>")
    for row in rows:
        lines.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(str(row["identity_key"])),
                html.escape(str(row["broker_id"])),
                html.escape(str(row["first_seen"])),
                html.escape(str(row["last_seen"])),
            )
        )
    lines.append("</table>")
    lines.append("</body></html>")
    return "\\n".join(lines)


'''

OLD_ANCHOR = 'PROFILE_PATH = "profile.json"'
NEW_ANCHOR = ROUTE + OLD_ANCHOR

assert OLD_IMPORT in t, "refimpl anchor (import block) not found -- did the target change?"
t = t.replace(OLD_IMPORT, NEW_IMPORT, 1)
assert OLD_ANCHOR in t, "refimpl anchor (PROFILE_PATH) not found -- did the target change?"
t = t.replace(OLD_ANCHOR, NEW_ANCHOR, 1)

p.write_text(t)
print("refimpl applied")
