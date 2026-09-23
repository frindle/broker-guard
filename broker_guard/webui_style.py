"""Shared presentation layer for the webUI: one page shell, a small set of
reusable HTML components (stat chip, badge, card, donut/stacked-bar chart,
horizontal stepper) and the CSS all of them share.

Deliberately plain Python string-building, same convention every route in
``webui.py`` already used before this module existed -- no Jinja2, no
``StaticFiles``/``aiofiles`` mount, no build step. The only external asset is
Chart.js from a CDN (for the two small charts on the dashboard); everything
else -- fonts aside -- is inlined so the app has zero new runtime
dependencies. Every value a caller passes in here is expected to already be
``html.escape``-d by the caller for anything that can contain user data;
this module's own literal chrome (labels, nav links) never needs it.
"""
import json

CHART_JS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"

FONTS_LINK = (
    "https://fonts.googleapis.com/css2?"
    "family=Fraunces:opsz,wght@9..144,500;9..144,600"
    "&family=IBM+Plex+Sans:wght@400;500;600"
    "&display=swap"
)

# Verification-kind categorical palette -- fixed order, never cycled, one hue
# per kind in broker_guard.brokers.VALID_KINDS plus the 'manual_review'
# fallback brokers.verification_kind() can return for an invalid kind.
KIND_ORDER = ("automatable", "captcha", "photo_id", "kba", "manual_review")
KIND_COLORS = {
    "automatable": "#2a78d6",
    "captcha": "#eb6834",
    "photo_id": "#1baf7a",
    "kba": "#eda100",
    "manual_review": "#e87ba4",
}
KIND_LABELS = {
    "automatable": "Automatable",
    "captcha": "CAPTCHA",
    "photo_id": "Photo ID",
    "kba": "Security question",
    "manual_review": "Manual review",
}

# Autopilot routing-decision palette -- what autopilot.decide_action actually
# does with a broker, fixed order.
ACTION_ORDER = ("auto_send", "needs_document", "needs_review")
ACTION_COLORS = {
    "auto_send": "#0E7C6B",
    "needs_document": "#B4690E",
    "needs_review": "#6B5FB0",
}
ACTION_LABELS = {
    "auto_send": "Auto-sent",
    "needs_document": "Needs a document",
    "needs_review": "Needs review",
}

# removal_status -> badge tone. None/unrecognized falls back to 'neutral'.
STATUS_TONE = {
    None: "neutral",
    "pending": "progress",
    "submitted": "progress",
    "needs_document": "action",
    "needs_review": "action",
    "confirmed": "success",
}

NAV_ITEMS = (
    ("dashboard", "/", "Dashboard"),
    ("brokers", "/brokers", "Brokers"),
    ("action", "/brokers#action-needed", "Action needed"),
    # One nav entry, not two: /identity IS the profiles list now. /profiles
    # still redirects there for old bookmarks, but giving it its own tab is
    # what made people think they were two separate places to manage
    # identity. Plural label, because every profile is equally real now --
    # there is no single "your profile" to point at.
    ("identity", "/identity", "Profiles"),
    ("exposure", "/exposure", "Exposure"),
    ("freeze", "/freeze", "Credit freeze"),
    # The runtime knobs that used to be editable ONLY as env vars in the
    # tracked docker-compose.yml -- and therefore silently reverted by every
    # `git reset --hard` redeploy. See broker_guard/settings.py.
    ("settings", "/settings", "Settings"),
)

PAGE_CSS = """
:root {
  --bg: #F7F6F3; --card: #FFFFFF; --border: #E5E3DD; --ink: #1A1A18;
  --muted: #6B6A65; --faint: #8A8981; --teal: #0E7C6B; --teal-ink: #0A5C50;
  --teal-fill: #EEF6F4; --input-border: #D8D6CF; --input-bg: #FCFCFB;
  --success-ink: #2F7D4F; --success-fill: #E4F1E9;
  --action-ink: #B4690E; --action-fill: #FBF0DE;
  --escalated-ink: #B4322F; --escalated-fill: #F9E4E3;
  --neutral-ink: #6B6A65; --neutral-fill: #F1EFE9;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: 'IBM Plex Sans', system-ui, -apple-system, sans-serif;
}
a { text-decoration: none; color: inherit; }
h1, h2, h3, .serif { font-family: 'Fraunces', serif; letter-spacing: -0.01em; }
h1 { font-size: 28px; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 17px; font-weight: 600; margin: 0 0 14px; }
.muted { color: var(--muted); }
.faint { color: var(--faint); }
.topnav {
  height: 64px; box-sizing: border-box; padding: 0 32px; border-bottom: 1px solid var(--border);
  background: var(--card); display: flex; align-items: center; gap: 28px; position: sticky; top: 0; z-index: 10;
}
.brand { display: flex; align-items: center; gap: 10px; font-family: 'Fraunces', serif; font-size: 19px; font-weight: 600; }
.brand svg { flex-shrink: 0; }
.navlinks { flex-grow: 1; display: flex; gap: 4px; flex-wrap: wrap; }
.navlinks a {
  padding: 8px 13px; border-radius: 8px; font-size: 14px; font-weight: 500; color: var(--muted);
}
.navlinks a.active { font-weight: 600; background: var(--teal-fill); color: var(--teal-ink); }
.navbadge {
  display: inline-block; min-width: 17px; height: 17px; line-height: 17px; text-align: center;
  font-size: 10px; font-weight: 600; color: #fff; background: var(--action-ink); border-radius: 9px;
  padding: 0 5px; margin-left: 6px;
}
main { max-width: 1180px; margin: 0 auto; padding: 32px 32px 56px; }
.page-head { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; gap: 16px; flex-wrap: wrap; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 22px 24px; }
.grid-main { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; align-items: start; }
@media (max-width: 900px) { .grid-main { grid-template-columns: 1fr; } }
.chips { display: grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap: 12px; margin-bottom: 18px; }
@media (max-width: 900px) { .chips { grid-template-columns: repeat(2, minmax(0,1fr)); } }
.chip { border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; background: var(--card); }
.chip .v { font-family: 'Fraunces', serif; font-size: 24px; font-weight: 600; }
.chip .l { font-size: 12px; color: var(--muted); margin-top: 2px; }
.chip.tone-success { background: var(--success-fill); border-color: #cfe6d8; }
.chip.tone-success .v { color: var(--success-ink); }
.chip.tone-progress { background: var(--teal-fill); border-color: #d5e8e2; }
.chip.tone-progress .v { color: var(--teal-ink); }
.chip.tone-action { background: var(--action-fill); border-color: #ebd9b4; }
.chip.tone-action .v { color: var(--action-ink); }
.chip.tone-escalated { background: var(--escalated-fill); border-color: #ebc5c3; }
.chip.tone-escalated .v { color: var(--escalated-ink); }
.badge {
  display: inline-block; font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 20px;
  text-transform: capitalize; white-space: nowrap;
}
.badge.tone-neutral { background: var(--neutral-fill); color: var(--neutral-ink); }
.badge.tone-progress { background: var(--teal-fill); color: var(--teal-ink); }
.badge.tone-action { background: var(--action-fill); color: var(--action-ink); }
.badge.tone-success { background: var(--success-fill); color: var(--success-ink); }
.badge.tone-escalated { background: var(--escalated-fill); color: var(--escalated-ink); }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 7px; flex-shrink: 0; }
.legend-row { display: flex; align-items: center; justify-content: space-between; font-size: 13px; padding: 7px 0; border-bottom: 1px solid #F1EFE9; }
.legend-row:last-child { border-bottom: none; }
.legend-row .name { display: flex; align-items: center; }
.stackbar { display: flex; height: 10px; border-radius: 6px; overflow: hidden; margin: 10px 0 14px; background: var(--neutral-fill); }
table.dtable { width: 100%; border-collapse: collapse; }
.toolbar { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
.toolbar input[type=text], .toolbar select {
  height: 38px; padding: 0 12px; border: 1px solid var(--input-border); border-radius: 9px;
  background: var(--input-bg); font-size: 13px; font-family: inherit; color: var(--ink);
}
.toolbar input[type=text] { flex: 1; min-width: 180px; }
/* Broker / Profile / Verification / Status / Last update / chevron.
   The chevron column is fixed-width so the five data columns line up with
   the .rowhead header above them. */
.row-summary {
  display: grid; grid-template-columns: 2fr 1fr 1fr 1fr 1fr 20px; gap: 14px; align-items: center;
  padding: 13px 16px; border-bottom: 1px solid #F1EFE9; cursor: pointer; list-style: none;
}
.row-summary::-webkit-details-marker { display: none; }
.row-summary .name { font-weight: 600; font-size: 14px; }
.row-summary .sub { font-size: 12px; color: var(--faint); }
.brokerrow[open] .row-summary { background: #FBFBF9; }
.chevron { transition: transform 0.15s ease; color: var(--faint); }
.brokerrow[open] .chevron { transform: rotate(90deg); }
.row-detail { padding: 4px 16px 22px 16px; border-bottom: 1px solid var(--border); background: #FBFBF9; }
/* /brokers "Scan results": one flat row per broker in the whole roster --
   not expandable, because there is no per-broker detail to expand into;
   the outcome badge IS the content. */
.scanrow {
  display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: 14px; align-items: center;
  padding: 11px 16px; border-bottom: 1px solid #F1EFE9;
}
.scanrow .name { font-weight: 600; font-size: 14px; }
.scanrow .sub { font-size: 12px; color: var(--faint); }
/* Column headers. Same grid as the rows beneath them (the class is applied
   alongside .row-summary / .scanrow), restyled as a header strip and with
   the row's click-to-expand cursor suppressed. */
.rowhead {
  cursor: default; background: #FBFBF9; border-bottom: 1px solid var(--border);
  font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--faint); padding-top: 9px; padding-bottom: 9px;
}
.rowhead .sortable { cursor: pointer; user-select: none; display: inline-flex; align-items: center; gap: 4px; }
.rowhead .sortable:hover { color: var(--teal-ink); }
/* The arrow is empty until sortBy() fills it, so unsorted headers do not
   imply an order that is not actually applied. */
.sortarrow { font-size: 9px; line-height: 1; color: var(--teal); }
#scanRowsContainer { max-height: 620px; overflow-y: auto; }
.stepper { display: flex; align-items: flex-start; margin: 14px 0 18px; }
.step { flex: 1; text-align: left; position: relative; }
.step .circle {
  width: 22px; height: 22px; border-radius: 50%; border: 2px solid var(--input-border); background: #fff;
  display: flex; align-items: center; justify-content: center; margin-bottom: 8px; font-size: 11px; color: var(--faint);
}
.step.done .circle { background: var(--teal); border-color: var(--teal); color: #fff; }
.step .bar { position: absolute; top: 10px; left: 22px; right: -8px; height: 2px; background: var(--input-border); }
.step.done .bar { background: var(--teal); }
.step:last-child .bar { display: none; }
.step .label { font-size: 12px; font-weight: 600; }
.step .at { font-size: 11px; color: var(--faint); margin-top: 1px; }
.step .note { font-size: 12px; color: var(--action-ink); margin-top: 6px; max-width: 220px; }
.pii-grid { display: grid; grid-template-columns: 140px 1fr; gap: 6px 14px; font-size: 13px; max-width: 480px; }
.pii-grid dt { color: var(--muted); }
.pii-grid dd { margin: 0; }
.notif { display: flex; justify-content: space-between; gap: 12px; padding: 11px 0; border-bottom: 1px solid #F1EFE9; font-size: 13px; }
.notif:last-child { border-bottom: none; }
.notif .when { color: var(--faint); font-size: 12px; white-space: nowrap; }
.encnote { display: flex; gap: 8px; align-items: flex-start; color: var(--teal-ink); font-size: 12px; background: var(--teal-fill); border-radius: 8px; padding: 9px 12px; margin-bottom: 18px; }
.inp {
  height: 40px; box-sizing: border-box; padding: 0 12px; border: 1px solid var(--input-border);
  border-radius: 9px; font-size: 14px; background: var(--input-bg); width: 100%; font-family: inherit;
}
textarea.inp { height: auto; padding: 10px 12px; line-height: 1.5; resize: vertical; }
.field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.field label { font-size: 13px; font-weight: 500; }
/* One button treatment for every button-shaped thing, <button> or <a>.
   inline-flex + centering is what makes an <a class="btn"> actually look
   like a button: height/padding do nothing on an inline element, which is
   why the "Edit"/"Cancel" links used to render as bare underlined text
   next to real buttons. */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  height: 40px; padding: 0 18px; border: 1px solid transparent; border-radius: 10px;
  background: var(--teal); color: #fff; text-decoration: none; white-space: nowrap;
  font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit;
  transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}
.btn:hover { background: var(--teal-ink); }
.btn:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; }
.btn.secondary { background: #fff; border-color: var(--input-border); color: var(--ink); }
.btn.secondary:hover { background: var(--teal-fill); border-color: var(--teal); color: var(--teal-ink); }
.btn.small { height: 32px; padding: 0 12px; font-size: 12px; border-radius: 8px; }
.btn.danger:hover { background: var(--escalated-fill); border-color: #e3b6b4; color: var(--escalated-ink); }
.btn:disabled, .btn[disabled] { opacity: 0.45; cursor: not-allowed; }
.btn:disabled:hover, .btn[disabled]:hover { background: var(--teal); }
.btn.secondary:disabled:hover, .btn.secondary[disabled]:hover { background: #fff; border-color: var(--input-border); color: var(--ink); }
/* Button groups (profile row actions, form save/cancel): the forms that
   wrap single buttons are display:contents so they do not break the row. */
.rowactions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rowactions form { display: contents; }
.section-label { font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--faint); margin-bottom: 14px; }
"""


def escape_attr(value: str) -> str:
    import html as _html
    return _html.escape(str(value), quote=True)


def _brand_svg() -> str:
    return (
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#0E7C6B" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg>'
    )


def render_page(title: str, active: str, body_html: str, action_needed_count: int = 0,
                 extra_head: str = "") -> str:
    """Wrap *body_html* in the shared page shell: fonts, CSS, top nav, Chart.js.

    ``active`` matches the first element of a ``NAV_ITEMS`` tuple to highlight
    the current section. ``action_needed_count`` is the real count of brokers
    whose ``removal_status`` is ``needs_document``/``needs_review`` right now
    (0 hides the badge) -- never a placeholder number.
    """
    nav = []
    for key, href, label in NAV_ITEMS:
        cls = "active" if key == active else ""
        badge_html = ""
        if key == "action" and action_needed_count > 0:
            badge_html = '<span class="navbadge">{}</span>'.format(action_needed_count)
        nav.append('<a class="{}" href="{}">{}{}</a>'.format(cls, href, label, badge_html))

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>{title} · Broker Guard</title>"
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="{fonts}" rel="stylesheet">'
        '<script src="{chartjs}"></script>'
        "<style>{css}</style>{extra_head}"
        "</head><body>"
        '<div class="topnav"><div class="brand">{svg}<span>Broker Guard</span></div>'
        '<div class="navlinks">{nav}</div></div>'
        "<main>{body}</main>"
        "</body></html>"
    ).format(
        title=title, fonts=FONTS_LINK, chartjs=CHART_JS_CDN, css=PAGE_CSS,
        extra_head=extra_head, svg=_brand_svg(), nav="".join(nav), body=body_html,
    )


def stat_chip(value, label: str, tone: str = "neutral") -> str:
    return (
        '<div class="chip tone-{tone}"><div class="v">{value}</div>'
        '<div class="l">{label}</div></div>'
    ).format(tone=tone, value=value, label=label)


def badge(text: str, tone: str = "neutral") -> str:
    return '<span class="badge tone-{tone}">{text}</span>'.format(tone=tone, text=text)


def status_badge(removal_status: str | None) -> str:
    tone = STATUS_TONE.get(removal_status, "neutral")
    text = removal_status or "not submitted"
    return badge(text, tone)


def legend_row(color: str, name: str, count: int) -> str:
    return (
        '<div class="legend-row"><span class="name">'
        '<span class="dot" style="background:{color}"></span>{name}</span>'
        "<strong>{count}</strong></div>"
    ).format(color=color, name=name, count=count)


def stacked_bar(segments: list[tuple[str, int, str]]) -> str:
    """*segments* is ``[(color, count, label), ...]``. Empty/all-zero input
    renders an empty (neutral) bar rather than dividing by zero."""
    total = sum(count for _, count, _ in segments) or 1
    parts = []
    for color, count, label in segments:
        if count <= 0:
            continue
        pct = 100.0 * count / total
        parts.append('<div style="width:{:.3f}%;background:{}" title="{}: {}"></div>'.format(
            pct, color, label, count,
        ))
    return '<div class="stackbar">{}</div>'.format("".join(parts))


def donut_canvas(canvas_id: str, labels: list[str], data: list[int], colors: list[str],
                  center_label: str) -> str:
    """A Chart.js doughnut with the total centered via an HTML overlay (not a
    chartjs-plugin) -- keeps the CDN dependency to just chart.js itself."""
    total = sum(data)
    config = {
        "type": "doughnut",
        "data": {"labels": labels, "datasets": [{"data": data, "backgroundColor": colors,
                                                   "borderWidth": 2, "borderColor": "#FFFFFF"}]},
        "options": {
            "cutout": "72%",
            "plugins": {"legend": {"display": False}},
            "animation": {"duration": 300},
        },
    }
    return (
        '<div style="position:relative;width:180px;height:180px;margin:0 auto;">'
        '<canvas id="{cid}" width="180" height="180"></canvas>'
        '<div style="position:absolute;inset:0;display:flex;flex-direction:column;'
        'align-items:center;justify-content:center;pointer-events:none;">'
        '<div style="font-family:\'Fraunces\',serif;font-size:26px;font-weight:600;">{total}</div>'
        '<div style="font-size:11px;color:var(--faint);text-align:center;max-width:100px;">{clabel}</div>'
        "</div></div>"
        "<script>new Chart(document.getElementById('{cid}'), {cfg});</script>"
    ).format(cid=canvas_id, total=total, clabel=center_label, cfg=json.dumps(config))


def area_chart(canvas_id: str, labels: list[str], data: list[int], series_label: str) -> str:
    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": series_label, "data": data, "borderColor": "#0E7C6B",
                "backgroundColor": "rgba(14,124,107,0.12)", "fill": True, "tension": 0.25,
                "pointRadius": 2, "borderWidth": 2,
            }],
        },
        "options": {
            "plugins": {"legend": {"display": False}},
            "scales": {
                "y": {"beginAtZero": True, "ticks": {"precision": 0},
                      "grid": {"color": "#E1E0D9"}},
                "x": {"grid": {"display": False}},
            },
        },
    }
    return (
        '<div style="height:220px;"><canvas id="{cid}"></canvas></div>'
        "<script>new Chart(document.getElementById('{cid}'), {cfg});</script>"
    ).format(cid=canvas_id, cfg=json.dumps(config))


def stepper(steps: list[dict]) -> str:
    """*steps* -- ``[{'label', 'done', 'at', 'note'}, ...]``, in order."""
    out = ['<div class="stepper">']
    for step in steps:
        cls = "step done" if step.get("done") else "step"
        mark = "&#10003;" if step.get("done") else ""
        at = '<div class="at">{}</div>'.format(step["at"]) if step.get("at") else '<div class="at">&nbsp;</div>'
        note = '<div class="note">{}</div>'.format(step["note"]) if step.get("note") else ""
        out.append(
            '<div class="{cls}"><div class="bar"></div><div class="circle">{mark}</div>'
            '<div class="label">{label}</div>{at}{note}</div>'.format(
                cls=cls, mark=mark, label=step["label"], at=at, note=note,
            )
        )
    out.append("</div>")
    return "".join(out)
