"""Generate DATASET_DEFECTS.md from the flags already written into the mapping entries.

WHY THIS IS GENERATED AND NOT HAND-MAINTAINED
---------------------------------------------
The mapping sweep records a defect at the moment it is found, in the prose of
the broker's own ``search_forms``/``optout_forms`` entry, because that is where
the evidence lives and where the next person to touch that broker will read it.
A separate hand-curated list would start drifting from those entries the first
time one was revised, and a stale defect list is worse than none: it sends a
research agent to verify things that were already resolved.

So this script EXTRACTS rather than restates. Every line below the summary is a
verbatim paragraph from an entry. If an entry is corrected, rerun this and the
list corrects itself.

WHAT IT CANNOT DO, STATED PLAINLY
---------------------------------
Detection is by keyword over English prose, so it is neither complete nor
perfectly precise:

* An entry that describes a defect without using one of the marker phrases in
  ``DATASET_MARKERS``/``BROKER_MARKERS``/``UNREACHABLE_MARKERS`` will be MISSED.
  Four were missed on the first run of this script and only found by checking a
  hand-written list of known cases against the output -- so if you add a defect
  class, check it appears rather than assuming it will.
* The ``kind:`` tag on each finding is a keyword guess, not a verified
  classification. It is there to let a research agent batch similar work, and
  it should be re-read against the quoted paragraph before being trusted.

Treat the output as a worklist assembled from the sweep's own notes, not as an
audited register. The count this prints is the useful drift signal: if it moves
without the sweep having advanced, something was edited.

Usage:  python3 gen_dataset_defects.py
"""
import re

from broker_guard import optout_forms as of
from broker_guard import search_forms as sf

# Phrases the sweep actually uses when flagging a problem with the DATASET ROW
# (wrong domain, dead URL, wrong company, entities conflated).
DATASET_MARKERS = (
    r"DATASET DEFECT",
    r"DATASET NOTE",
    r"dataset defect",
    r"dataset note",
    r"flagged (?:and )?(?:deliberately )?not fixed",
    r"flagged,? (?:and )?(?:deliberately )?not fixed",
    r"flagged here and left unfixed",
    r"flagged here and (?:deliberately )?not fixed",
    r"dataset-shape oddity",
)

# Phrases used when the defect is in the BROKER'S OWN published surface -- a
# broken rights link, an unconfigured template, a form that asks the wrong
# question. These are not dataset errors and are not fixable by editing
# source-brokers.json, but Penn wants them on the same worklist.
BROKER_MARKERS = (
    r"A DEFECT ON THE PAGE ITSELF",
    r"defect in the BROKER'S OWN",
    r"UNREPLACED COOKIEBOT",
    r"unreplaced Cookiebot",
    r"EMPTY href",
    r"empty href",
    r"circular reference",
    r"WRONG-REQUEST-TYPE",
    r"wrong-request-type",
    r"BROKEN --",
)

# A marker inside one of these means the paragraph is pointing AT the other
# leg's entry rather than reporting a new finding. Recorded once, not twice.
CROSS_REF = re.compile(
    r"(?:Recorded alongside|see|See) [^.]{0,60}"
    r"(?:on the (?:opt-out|search) leg|opt-out entry|search entry)",
    re.I,
)

# Coarse buckets, first match wins. Ordered most-specific first.
KINDS = (
    ("entity-mismatch",
     r"different company|unrelated compan|wrong row|conflates two|"
     r"belong to the compan|not a broker at all|three-entity|"
     r"three entities|KEYED to|keyed to the old domain"),
    ("rebrand-or-domain-change",
     r"rebrand|renamed|domain change|now trading as|acquired|"
     r"redirects to [a-z0-9.-]+\.(?:com|ai|io)"),
    ("parked-or-defunct",
     r"parked domain|listed for sale|for-sale listing|LEFT THE BUSINESS|"
     r"has shut down|not launched|Coming Soon|Account Suspended|suspended"),
    ("dead-url",
     r"HTTP 404|returns a hard HTTP 404|genuine HTTP 404|404 \(|"
     r"never renders|could not be loaded|ERR_CERT|expired"),
    ("stale-200",
     r"still returns HTTP 200 but|no longer contains a form|"
     r"stopped being the page"),
    ("broker-surface-defect",
     r"EMPTY href|empty href|circular reference|UNREPLACED|unreplaced|"
     r"wrong-request-type|WRONG-REQUEST-TYPE|BROKEN"),
    ("contact-address-oddity",
     r"personal rather than a role address|personal address|"
     r"contact is [a-z0-9._%+-]+@|operations address, not a privacy one"),
)

DICTS = (
    ("optout", of, ("RECIPES", "STAGED_RECIPES", "NO_OPTOUT_SURFACE",
                    "OPTOUT_UNDECIDED", "OPTOUT_BLOCKED",
                    "OPTOUT_OUT_OF_SCOPE")),
    ("search", sf, ("RECIPES", "NO_SEARCH_SURFACE", "SEARCH_UNDECIDED",
                    "SEARCH_BLOCKED")),
)

# A third class: the company or its site cannot be reached at all -- parked and
# for-sale domains, suspended hosting, expired certificates, sites that never
# launched, companies that have closed. These name no "defect" as such, but they
# are exactly the rows a research agent must recheck, because every one of them
# can change state without notice. A parked domain is the sharpest case: if
# someone buys it, the recorded opt-out path could later belong to a stranger.
UNREACHABLE_MARKERS = (
    r"PARKED DOMAIN",
    r"parked domain",
    r"listed for sale",
    r"for-sale listing",
    r"Account Suspended",
    r"has been suspended",
    r"ERR_CERT",
    r"certificate is expired",
    r"expired or not yet valid",
    r"has not launched",
    r"Coming Soon",
    r"LEFT THE BUSINESS",
    r"genuinely defunct",
)

_DATASET_RE = re.compile("|".join(DATASET_MARKERS))
_BROKER_RE = re.compile("|".join(BROKER_MARKERS))
_UNREACHABLE_RE = re.compile("|".join(UNREACHABLE_MARKERS))


def classify(text, scope):
    # When the finding is about the broker's own surface, prefer the
    # surface-defect bucket: these paragraphs often also mention a rebrand or a
    # dead URL in passing, and first-match-wins would file them under that
    # instead of under the thing that actually needs reporting.
    if scope == "broker-surface":
        for name, pattern in KINDS:
            if name == "broker-surface-defect" and re.search(pattern, text, re.I):
                return name
    if scope == "unreachable":
        return "parked-or-defunct"
    for name, pattern in KINDS:
        if re.search(pattern, text, re.I):
            return name
    return "unclassified"


def collect():
    """Return {broker_id: [finding, ...]} extracted from the entries."""
    found = {}
    for leg, module, names in DICTS:
        for dict_name in names:
            entries = getattr(module, dict_name, {})
            for broker_id, value in entries.items():
                if not isinstance(value, str):
                    continue          # a FormRecipe, not prose
                for para in value.split("\n\n"):
                    is_dataset = bool(_DATASET_RE.search(para))
                    is_broker = bool(_BROKER_RE.search(para))
                    is_gone = bool(_UNREACHABLE_RE.search(para))
                    if not (is_dataset or is_broker or is_gone):
                        continue
                    if CROSS_REF.search(para):
                        continue      # points at the other leg; not new
                    if is_dataset:
                        scope = "dataset"
                    elif is_broker:
                        scope = "broker-surface"
                    else:
                        scope = "unreachable"
                    found.setdefault(broker_id, []).append({
                        "leg": leg,
                        "dict": dict_name,
                        "scope": scope,
                        "kind": classify(para, scope),
                        "text": " ".join(para.split()),
                    })
    return found


def main():
    found = collect()
    total = sum(len(v) for v in found.values())

    by_kind = {}
    by_scope = {"dataset": 0, "broker-surface": 0, "unreachable": 0}
    for findings in found.values():
        for f in findings:
            by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
            by_scope[f["scope"]] += 1

    out = []
    out.append("# Dataset and surface defects found by the mapping sweep")
    out.append("")
    out.append("GENERATED FILE -- do not edit by hand. Regenerate with")
    out.append("`python3 gen_dataset_defects.py`. Every quoted paragraph below is")
    out.append("verbatim from a broker's entry in `broker_guard/search_forms.py` or")
    out.append("`broker_guard/optout_forms.py`; fix the entry, not this file.")
    out.append("")
    out.append("## What this is for")
    out.append("")
    out.append("The sweep flags defects but deliberately DOES NOT fix")
    out.append("`data/source-brokers.json` -- a mapping pass that also edits its own")
    out.append("input cannot be audited afterwards. This file consolidates those flags")
    out.append("so they can be resolved as one piece of research (verify which company a")
    out.append("mismatched form really belongs to, recheck ownership on parked domains,")
    out.append("find the live URL behind a dead one) instead of staying scattered across")
    out.append("hundreds of entries.")
    out.append("")
    out.append("Two scopes are mixed here on purpose, and they need different remedies:")
    out.append("")
    out.append("* **dataset** -- the row in `source-brokers.json` is wrong or stale.")
    out.append("  Fixable by editing the dataset.")
    out.append("* **broker-surface** -- the broker's own published rights channel is")
    out.append("  broken (a dead link, an unconfigured template, a form asking the wrong")
    out.append("  question). NOT fixable from here; worth recording because it affects")
    out.append("  whether a person can exercise a right at all, and may be worth")
    out.append("  reporting to the broker or a regulator.")
    out.append("* **unreachable** -- the company or its site cannot be reached at all:")
    out.append("  parked or for-sale domains, suspended hosting, expired certificates,")
    out.append("  sites that never launched, companies that have closed. These need")
    out.append("  RECHECKING rather than fixing, and they are the most time-sensitive")
    out.append("  rows here. A parked domain is the sharpest case: if it is bought, the")
    out.append("  recorded opt-out path could later belong to a stranger, so a recheck")
    out.append("  must confirm OWNERSHIP and not merely that a page has appeared.")
    out.append("")
    out.append("## Caveats that matter before acting on this")
    out.append("")
    out.append("Findings are extracted from English prose by keyword, so this list is")
    out.append("**not complete** -- an entry that describes a defect in other words is")
    out.append("missed. The `kind:` tag is a keyword guess, not a verified")
    out.append("classification: read the quoted paragraph before trusting it. Nothing")
    out.append("here has been re-verified since the date stated inside each quote, and")
    out.append("several of these defects are the kind that resolve themselves (an")
    out.append("expired certificate gets renewed, a suspended host comes back).")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append("%d findings across %d brokers." % (total, len(found)))
    out.append("")
    out.append("| scope | findings |")
    out.append("| --- | --- |")
    for scope in sorted(by_scope):
        out.append("| %s | %d |" % (scope, by_scope[scope]))
    out.append("")
    out.append("| kind (keyword guess) | findings |")
    out.append("| --- | --- |")
    for kind in sorted(by_kind, key=lambda k: (-by_kind[k], k)):
        out.append("| %s | %d |" % (kind, by_kind[kind]))
    out.append("")
    out.append("## Findings by broker")
    out.append("")
    for broker_id in sorted(found):
        out.append("### `%s`" % broker_id)
        out.append("")
        for f in found[broker_id]:
            out.append("- **scope:** %s | **kind:** %s | **from:** "
                       "`%s_forms.%s`" % (f["scope"], f["kind"], f["leg"],
                                          f["dict"]))
            out.append("")
            out.append("  > %s" % f["text"])
            out.append("")

    with open("DATASET_DEFECTS.md", "w") as fh:
        fh.write("\n".join(out) + "\n")

    print("wrote DATASET_DEFECTS.md: %d findings across %d brokers "
          "(%d dataset, %d broker-surface, %d unreachable)"
          % (total, len(found), by_scope["dataset"],
             by_scope["broker-surface"], by_scope["unreachable"]))



if __name__ == "__main__":
    main()
