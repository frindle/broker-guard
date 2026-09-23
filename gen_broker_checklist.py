#!/usr/bin/env python3
"""Generate BROKER_MAPPING_CHECKLIST.md from source-brokers.json cross-referenced
against search_forms.py / optout_forms.py's RECIPES / NO_*_SURFACE / *_UNDECIDED dicts.

Purpose: let any fresh agent resume broker-by-broker opt-out/search mapping work
without needing session continuity -- pick the next unmapped broker off the list.
"""
import json
import re
import sys

sys.path.insert(0, ".")
from broker_guard import search_forms as sf
from broker_guard import optout_forms as of


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "broker"


def domain_id(domain):
    domain = (domain or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return slugify(domain)


SEARCH_STATE = {}
for k in sf.RECIPES:
    SEARCH_STATE[k] = "recipe"
for k in sf.NO_SEARCH_SURFACE:
    SEARCH_STATE[k] = "no-surface"
for k in sf.SEARCH_UNDECIDED:
    SEARCH_STATE[k] = "undecided"
# Also terminal: a search surface that exists and is walled by an anti-bot
# challenge on every visit. Decided, documented, and not a TODO -- so it must
# count as mapped, or the checklist sends the next agent to redo the research.
for k in getattr(sf, "SEARCH_BLOCKED", {}):
    SEARCH_STATE.setdefault(k, "blocked")

OPTOUT_STATE = {}
for k in of.RECIPES:
    OPTOUT_STATE[k] = "recipe"
for k in of.NO_OPTOUT_SURFACE:
    OPTOUT_STATE[k] = "no-surface"
for k in of.OPTOUT_UNDECIDED:
    OPTOUT_STATE[k] = "undecided"
# Two more terminal states this file used to ignore, which is why brokers that
# had in fact been decided kept showing up as UNMAPPED and inviting rework:
#   blocked      -- a real opt-out surface behind an anti-bot wall every visit
#   out-of-scope -- a real, reachable form asking for something this codebase
#                   will not do (ID upload, per-result picking, modal wizard)
for k in getattr(of, "OPTOUT_BLOCKED", {}):
    OPTOUT_STATE.setdefault(k, "blocked")
for k in getattr(of, "OPTOUT_OUT_OF_SCOPE", {}):
    OPTOUT_STATE.setdefault(k, "out-of-scope")
# A third: STAGED_RECIPES. The form IS transcribed element by element against
# the live page -- the research is finished -- it is simply not turned on,
# because "we wrote the form down" and "we are willing to submit to it" are
# two separate decisions. Counting it as UNMAPPED would send the next agent
# to re-transcribe a form this repo already holds in full.
for k in getattr(of, "STAGED_RECIPES", {}):
    OPTOUT_STATE.setdefault(k, "staged")

brokers = json.load(open("data/source-brokers.json"))

rows = []
for b in brokers:
    bid = domain_id(b.get("domain"))
    s = SEARCH_STATE.get(bid, "unmapped")
    o = OPTOUT_STATE.get(bid, "unmapped")
    rows.append({
        "id": bid,
        "name": b.get("name") or bid,
        "domain": b.get("domain") or "",
        "search": s,
        "optout": o,
        "done": s != "unmapped" and o != "unmapped",
    })

rows.sort(key=lambda r: r["name"].lower())

total = len(rows)
done = sum(1 for r in rows if r["done"])
search_mapped = sum(1 for r in rows if r["search"] != "unmapped")
optout_mapped = sum(1 for r in rows if r["optout"] != "unmapped")

MARK = {
    "recipe": "recipe",
    "no-surface": "no-surface",
    "undecided": "undecided",
    "blocked": "blocked",
    "out-of-scope": "out-of-scope",
    "staged": "staged",
    "unmapped": "UNMAPPED",
}

lines = []
lines.append("# Broker Mapping Checklist")
lines.append("")
lines.append("Resume state for the broker-by-broker search/opt-out mapping workstream --")
lines.append("cross-references `data/source-brokers.json` (the canonical %d-broker dataset)" % total)
lines.append("against every terminal dict in `broker_guard/search_forms.py` and")
lines.append("`broker_guard/optout_forms.py` -- `RECIPES`, `NO_*_SURFACE`, `*_UNDECIDED`,")
lines.append("`*_BLOCKED`, `OPTOUT_OUT_OF_SCOPE` and `STAGED_RECIPES`.")
lines.append("")
lines.append("**A broker is \"mapped\" once BOTH legs (search, opt-out) are categorized into one of")
lines.append("the terminal states below -- not just recipe.** Every state but `UNMAPPED` is a")
lines.append("legitimate resolution, not a TODO.")
lines.append("")
lines.append("- `recipe` -- concrete automatable recipe exists in `RECIPES`")
lines.append("- `no-surface` -- confirmed no honest search/opt-out surface exists (dead end, documented)")
lines.append("- `undecided` -- surface exists but not yet resolved to a recipe or a no-surface call")
lines.append("- `blocked` -- surface exists and is behind an anti-bot wall on every visit")
lines.append("- `out-of-scope` -- real reachable form, asking for something this codebase will not do")
lines.append("  (government-ID upload, picking your own record out of a result list, a modal wizard)")
lines.append("- `staged` -- opt-out form transcribed element by element from the live page,")
lines.append("  held in `STAGED_RECIPES` and deliberately not turned on (no human dry run)")
lines.append("- `UNMAPPED` -- not yet looked at for this leg at all")
lines.append("")
lines.append("**Regenerate this file** after mapping more brokers: `python3 gen_broker_checklist.py`")
lines.append("(script lives in this repo's tooling; ask if missing -- it's a short cross-reference")
lines.append("of those dicts against source-brokers.json).")
lines.append("")
lines.append("## Progress")
lines.append("")
lines.append("- Total brokers: **%d**" % total)
lines.append("- Fully mapped (both legs): **%d** / %d" % (done, total))
lines.append("- Search leg mapped: %d / %d" % (search_mapped, total))
lines.append("- Opt-out leg mapped: %d / %d" % (optout_mapped, total))
lines.append("")
lines.append("## How to resume")
lines.append("")
lines.append("1. Pick any `[ ]` row below (not yet fully mapped).")
lines.append("2. Research that broker's domain: does it have a people-search surface? An opt-out form/email?")
lines.append("3. Add a `RECIPES` entry (if automatable), or add it to the matching `NO_*_SURFACE`,")
lines.append("   `*_BLOCKED` or `OPTOUT_OUT_OF_SCOPE` dict, with a specific reason, in")
lines.append("   `search_forms.py` / `optout_forms.py` -- keyed by the broker's id in parens below.")
lines.append("4. If you can't fully resolve a leg this session, add it to `*_UNDECIDED` with what you")
lines.append("   found so far, so the next agent doesn't repeat the research.")
lines.append("5. Re-run the generator to flip this checklist's row to `[x]`.")
lines.append("")
lines.append("## Brokers")
lines.append("")

for r in rows:
    box = "x" if r["done"] else " "
    lines.append("- [%s] **%s** (`%s`) -- search: %s, opt-out: %s" % (
        box, r["name"], r["id"], MARK[r["search"]], MARK[r["optout"]]))

lines.append("")

with open("BROKER_MAPPING_CHECKLIST.md", "w") as f:
    f.write("\n".join(lines))

print("wrote BROKER_MAPPING_CHECKLIST.md: %d total, %d fully mapped, %d search-mapped, %d optout-mapped" % (
    total, done, search_mapped, optout_mapped))
