#!/usr/bin/env python3
"""Dry-run every STAGED recipe against its live page and report who passes.

Staged recipes were transcribed from the live DOM but never exercised. This
fills each one with the DUMMY identity in profile.example.json in a real
browser, presses nothing, and reports per recipe:

    pass     the form filled, a screenshot was taken, no bot check, no drift
    blocked  a bot wall or CAPTCHA stood in the way (inconclusive, NOT a pass)
    drift    a selector no longer matches (the recipe needs repair)
    error    anything else

Nothing is submitted and no real PII is used. Run it in the container (home IP,
headful) for a meaningful answer:

    docker compose exec broker-guard python tools/promote_staged.py [--ids a,b] [--out report.json]

A recipe is promoted by moving its entry from STAGED_RECIPES to RECIPES in
broker_guard/optout_forms.py (``_PROMOTED``); this tool only produces the
evidence and prints the ids that earned it.
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_guard import optout_forms, optout_submit, recipe_check, review  # noqa: E402
from broker_guard.config import Config  # noqa: E402
from broker_guard.profile import Identity  # noqa: E402

DUMMY = Identity(first_name="Jane", last_name="Doe", middle_name="Q",
                 emails=["jane.doe@example.com"], phones=["+1-555-0100"],
                 addresses=["12 Main St, Springfield, IL 62704"])


def verdict(record: dict, drift: dict | None) -> str:
    """Collapse a dry-run record and a selector check into one word."""
    if drift and drift.get("status") == "drift":
        return "drift"
    outcome = record.get("outcome")
    if outcome == review.OUTCOME_DRY_RUN:
        return "pass"
    if outcome == review.OUTCOME_NEEDS_MANUAL:
        return "blocked"
    return "error"


def run(recipes: dict, submitter, cfg, identity=DUMMY) -> dict:
    out = {}
    for broker_id, recipe in sorted(recipes.items()):
        page_for_check = None
        drift = None
        try:
            context, page_for_check = submitter.new_page()
            drift = recipe_check.check_page(
                page_for_check, recipe.url, recipe_check.selectors_for_optout(recipe))
        except Exception as exc:  # noqa: BLE001
            drift = {"status": "unknown", "reason": type(exc).__name__}
        finally:
            try:
                context.close()
            except Exception:
                pass
        try:
            record = optout_submit.submit_optout(
                recipe, identity, cfg, submitter=submitter, dry_run=True, allow_candidate=True)
        except Exception as exc:  # noqa: BLE001
            record = {"outcome": "error", "reason": type(exc).__name__}
        out[broker_id] = {"verdict": verdict(record, drift), "outcome": record.get("outcome"),
                          "reason": record.get("reason"), "detected": record.get("detected"),
                          "drift": {k: drift.get(k) for k in ("status", "missing", "ambiguous")}
                          if drift else None}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ids", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    ids = [i for i in args.ids.split(",") if i]
    recipes = {k: v for k, v in optout_forms.STAGED_RECIPES.items() if not ids or k in ids}
    review_dir = tempfile.mkdtemp(prefix="staged-dryrun-")
    cfg = Config(review_dir=review_dir, optout_submit_enabled=True, optout_submit_dry_run=True,
                 playwright_enabled=True,
                 playwright_headless=os.environ.get("BG_PLAYWRIGHT_HEADLESS", "false").lower() == "true",
                 browser_stealth=os.environ.get("BG_BROWSER_STEALTH", ""),
                 browser_profile_dir=os.environ.get("BG_BROWSER_PROFILE_DIR") or None)
    submitter = optout_submit.OptOutSubmitter(
        headless=cfg.playwright_headless, stealth=cfg.browser_stealth,
        profile_dir=cfg.browser_profile_dir)
    submitter.start()
    try:
        report = run(recipes, submitter, cfg)
    finally:
        submitter.close()
    text = json.dumps(report, indent=1, sort_keys=True)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    print("\nearned promotion: " + ", ".join(k for k, v in sorted(report.items())
                                            if v["verdict"] == "pass") or "none",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
