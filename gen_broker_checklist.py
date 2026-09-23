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

OPTOUT_STATE = {}
for k in of.RECIPES:
    OPTOUT_STATE[k] = "recipe"
for k in of.NO_OPTOUT_SURFACE:
    OPTOUT_STATE[k] = "no-surface"
for k in of.OPTOUT_UNDECIDED:
    OPTOUT_STATE[k] = "undecided"

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

MARK = {"recipe": "recipe", "no-surface": "no-surface", "undecided": "undecided", "unmapped": "UNMAPPED"}

lines = []
lines.append("# Broker Mapping Checklist")
lines.append("")
lines.append("Resume state for the broker-by-broker search/opt-out mapping workstream --")
lines.append("cross-references `data/source-brokers.json` (the canonical %d-broker dataset)" % total)
lines.append("against `broker_guard/search_forms.py`'s `RECIPES`/`NO_SEARCH_SURFACE`/`SEARCH_UNDECIDED`")
lines.append("and `broker_guard/optout_forms.py`'s `RECIPES`/`NO_OPTOUT_SURFACE`/`OPTOUT_UNDECIDED`.")
lines.append("")
lines.append("**A broker is \"mapped\" once BOTH legs (search, opt-out) are categorized into one of")
lines.append("the three terminal states below -- not just recipe.** `no-surface` and `undecided` are")
lines.append("legitimate resolutions, not TODOs.")
lines.append("")
lines.append("- `recipe` -- concrete automatable recipe exists in `RECIPES`")
lines.append("- `no-surface` -- confirmed no honest search/opt-out surface exists (dead end, documented)")
lines.append("- `undecided` -- surface exists but not yet resolved to a recipe or a no-surface call")
lines.append("- `UNMAPPED` -- not yet looked at for this leg at all")
lines.append("")
lines.append("**Regenerate this file** after mapping more brokers: `python3 gen_broker_checklist.py`")
lines.append("(script lives in this repo's tooling; ask if missing -- it's a ~70-line cross-reference")
lines.append("of the three RECIPES/*_SURFACE/*_UNDECIDED dicts against source-brokers.json).")
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
lines.append("3. Add a `RECIPES` entry (if automatable), or add it to the matching `NO_*_SURFACE` dict")
lines.append("   (if genuinely no surface exists, with a one-line reason), in `search_forms.py` /")
lines.append("   `optout_forms.py` -- keyed by the broker's id shown in parens below.")
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
