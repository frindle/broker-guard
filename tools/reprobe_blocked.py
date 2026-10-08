#!/usr/bin/env python3
"""Re-probe every brokers recorded as walled (OPTOUT_BLOCKED / SEARCH_BLOCKED).

The walls in the mapping were recorded with a headless, stale-UA, throwaway
browser. Some were fingerprint false positives. Run this inside the
broker-guard container (headful under Xvfb, native UA, persistent profile,
home IP) and it re-loads each walled page and reports which are now OPEN.

It only LOADS pages (GET). It fills nothing and submits nothing, and needs no
profile / PII. Output is JSON on stdout: {"open": [...], "still_walled": {...},
"errors": {...}}. Pure helper ``classify`` is unit-tested; the browser is
injected so tests never launch one.

    python tools/reprobe_blocked.py [--stealth patchright] [--limit N] [--which optout|search|both]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_guard.browser import bot_wall_reason  # noqa: E402


def blocked_ids(which: str = "both") -> list:
    ids = []
    if which in ("optout", "both"):
        from broker_guard.optout_forms import OPTOUT_BLOCKED
        ids += list(OPTOUT_BLOCKED)
    if which in ("search", "both"):
        from broker_guard.search_forms import SEARCH_BLOCKED
        ids += list(SEARCH_BLOCKED)
    return list(dict.fromkeys(ids))


def classify(text, title, status) -> str:
    """'open' when the page is broker content, else the wall reason."""
    return bot_wall_reason(text, title, status) or "open"


def reprobe(ids, urls: dict, session, timeout_ms: int = 30000) -> dict:
    """Load each id's URL via *session* (a ``BrowserSession``-like object)."""
    out = {"open": [], "still_walled": {}, "errors": {}, "no_url": []}
    for bid in ids:
        url = urls.get(bid)
        if not url:
            out["no_url"].append(bid)
            continue
        context = page = None
        try:
            context = session.new_context(timeout_ms=timeout_ms)
            page = context.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)
            verdict = classify(page.inner_text("body"), page.title(),
                               resp.status if resp else None)
            if verdict == "open":
                out["open"].append(bid)
            else:
                out["still_walled"][bid] = verdict
        except Exception as exc:  # the failure is the finding
            out["errors"][bid] = type(exc).__name__
        finally:
            for obj in (page, context):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stealth", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--which", default="both", choices=("optout", "search", "both"))
    ap.add_argument("--brokers", default=os.path.join("data", "brokers.json"))
    args = ap.parse_args(argv)
    from broker_guard.browser_launch import BrowserSession

    with open(args.brokers) as fh:
        rows = json.load(fh)
    rows = rows.get("brokers", rows) if isinstance(rows, dict) else rows
    urls = {}
    for row in rows:
        urls[row["id"]] = row.get("optout_url") or row.get("url")
    ids = blocked_ids(args.which)
    if args.limit:
        ids = ids[:args.limit]
    session = BrowserSession(
        headless=os.environ.get("BG_PLAYWRIGHT_HEADLESS", "false").lower() == "true",
        stealth=args.stealth or os.environ.get("BG_BROWSER_STEALTH", ""),
        profile_dir=os.environ.get("BG_BROWSER_PROFILE_DIR") or None,
        leg="reprobe",
        user_agent=os.environ.get("BG_BROWSER_USER_AGENT") or None).start()
    try:
        print(json.dumps(reprobe(ids, urls, session), indent=1, sort_keys=True))
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
