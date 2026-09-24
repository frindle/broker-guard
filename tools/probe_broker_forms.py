#!/usr/bin/env python3
"""Render broker pages in a real browser and describe every form on them.

WHY THIS EXISTS, and why a static fetcher is not good enough
-----------------------------------------------------------
The broker-mapping workstream originally researched pages with a static
fetcher. On 2026-09-23 that method was caught producing WRONG answers, not
merely incomplete ones: two brokers had been recorded as carrying NO CAPTCHA
-- ``accurateappend-com`` and ``atdata-com``, at the time the two best recipe
candidates in the dataset -- and rendering the same pages in a browser the
same day showed a Cloudflare Turnstile on one and a reCAPTCHA on the other.
Had either shipped as a ``FormRecipe`` it would have been a fabricated entry,
which ``optout_forms``' own docstring calls worse than an honest "undecided".

The standing rule that came out of it, and the reason this file lives in the
repo rather than in somebody's /tmp:

    NEVER call "no bot check" from a static fetch.

Bot checks routinely do not exist in the served HTML. Invisible reCAPTCHA v3
and reCAPTCHA Enterprise inject only a hidden textarea once their script runs;
Cloudflare Turnstile's widget id is minted per page load; Elementor and
Gravity Forms attach their captcha field lazily. A fetcher sees none of it.
The same goes for the forms themselves: consent portals (OneTrust, TrustArc,
DataGrail, Ketch, Osano, Termly), React SPAs and Wix pages serve an empty
shell and build the form client-side.

WHAT IT REPORTS
---------------
Per URL: HTTP status, the URL actually landed on after redirects, page title,
and for every ``<form>`` its id/class/action/method plus, for each control,
the tag, type, ``name``, ``id``, ``class``, whether it is required, whether it
is off-layout, its label (from ``<label for>``, else placeholder, else
aria-label), button text, checkbox/radio value, and a select's option count
and first few options. Controls OUTSIDE any form are reported separately --
consent-banner buttons and stray ``g-recaptcha-response`` textareas both show
up there, and both are findings.

It also flags, explicitly:

* ``cap``     -- captcha SCRIPTS the page loaded (reCAPTCHA incl. Enterprise,
                 hCaptcha, Turnstile, BotDetect, Friendly Captcha, Arkose)
* ``widget``  -- captcha ELEMENTS present in the DOM. Checked separately
                 because the element can be there before the script fetches,
                 which is exactly how ``bdex-com`` was caught.
* ``invis``   -- per control, set when it computes to display:none,
                 visibility:hidden, opacity:0 or is parked off-screen at a
                 large negative left. This is how HONEYPOTS announce
                 themselves, and a honeypot must end up in a recipe's
                 ``forbidden_selectors`` or the submission is flagged as a bot.
* ``links``   -- anchors whose text or href mentions opt-out/do-not-sell/
                 privacy-choices/remove/request. Load-bearing, because a
                 recurring finding is that the dataset's recorded
                 ``opt_out_url`` is a rights EXPLAINER with no inputs on it,
                 or a consent-portal DSAR form that explicitly refuses
                 do-not-sell requests, and the real surface is a footer link.
* ``frames``  -- the same description again, per CHILD FRAME, for any frame
                 that holds controls or loads a captcha. Brokers routinely
                 EMBED their request form (OneTrust, ServiceNow, a vendor
                 portal) rather than serving it, and the main frame then
                 reports zero forms on a page that visibly has one. A frame
                 this browser may not read is still listed, marked
                 ``unreadable``, because "there is a cross-origin form here"
                 is a finding and silence is not.

                 THE HAZARD: ``frame.evaluate`` accepts no timeout and can
                 hang indefinitely on a frame whose main thread is wedged.
                 ``faraday.io`` did exactly that on 2026-09-23 and took a
                 whole 16-target run down with it -- eight targets had been
                 probed successfully and all eight were lost, because
                 results were only written at the end. Both halves of that
                 are now fixed: ``TARGET_BUDGET_S`` caps each target, and
                 the output file is rewritten after every one of them.

READ-ONLY, WITH ONE HONEST CAVEAT
---------------------------------
Nothing is ever typed, clicked or submitted. The browser navigates, waits for
the page to settle, reads the DOM and closes. A broker's opt-out form must
never be submitted by a reconnaissance pass.

The caveat, found on 2026-09-23 and recorded because "read-only" was being
claimed more strongly than the code can deliver: SOME OPT-OUTS ARE ACTUATED BY
THE NAVIGATION ITSELF. Requesting ``dstillery.com/optout`` -- no form on it,
nothing clicked -- redirected straight to an ``?success=1`` page reading "You
will no longer receive targeted advertisements from Dstillery on this
browser". The opt-out was a bare GET, and merely looking performed it.

Not typing and not clicking is therefore not the same as not acting. The
consequence here was harmless (a throwaway browser profile was opted out of ad
targeting), but the shape generalises: any URL with "optout", "unsubscribe",
"remove" or "confirm" in it may be a one-click action link rather than a page,
and a confirmation link mailed to a real person is the case where this would
do genuine damage. Probe such URLs deliberately, one at a time, knowing they
may fire -- never in a bulk sweep.

USAGE
-----
    python3 tools/probe_broker_forms.py targets.json out.json
    python3 tools/probe_broker_forms.py targets.json out.json --head

``targets.json`` is ``[[broker_id, url], ...]``. Output is a JSON object keyed
by broker_id. Errors are recorded per target rather than raised: a DNS failure
(``nuwber.com``) or a certificate whose name does not match the host
(``optout.prod.bidr.io``) IS the finding, and is worth distinguishing in the
written-up entry from an anti-bot wall.

Roughly fifteen URLs per invocation keeps the output readable in one screen.
"""
import json
import os
import subprocess
import sys

from playwright.sync_api import sync_playwright

# How long one target may take in total before it is abandoned. This is a
# WALL-CLOCK cap over the whole per-target block, not a per-call timeout,
# because the hang this exists for was not in any call that takes one.
TARGET_BUDGET_S = 75


# Reads the rendered DOM. Kept as one expression so it can be handed to
# page.evaluate() unchanged, and deliberately defensive: className is not a
# string on SVG elements, and a broker page is exactly where that turns up.
JS = r"""
() => {
  const cls = (e) => (typeof e.className === 'string' ? e.className : '');
  const pick = (e) => {
    let lab = '';
    if (e.id) {
      const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
      if (l) lab = l.innerText.trim().replace(/\s+/g, ' ').slice(0, 55);
    }
    if (!lab) lab = (e.placeholder || e.getAttribute('aria-label') || '').slice(0, 55);
    const st = getComputedStyle(e);
    const o = {t: e.tagName, ty: e.type, n: e.name, id: e.id,
               cl: cls(e).slice(0, 40),
               req: !!(e.required || e.getAttribute('aria-required') === 'true'),
               off: (e.offsetParent === null), lab};
    // Honeypot tell. Any of these four and the control is not meant for a
    // human to fill, so a recipe must decline it explicitly.
    if (st.display === 'none' || st.visibility === 'hidden' ||
        st.opacity === '0' || parseInt(st.left || '0') < -999) o.invis = 1;
    if (e.tagName === 'BUTTON') o.txt = e.innerText.trim().slice(0, 40);
    if (e.type === 'submit' && e.value) o.txt = e.value.slice(0, 40);
    if (e.type === 'checkbox' || e.type === 'radio') o.v = (e.value || '').slice(0, 60);
    if (e.tagName === 'SELECT') {
      o.nopt = e.options.length;
      o.opt = [...e.options].map(x => x.text.slice(0, 24)).slice(0, 6);
    }
    return o;
  };
  const sel = 'input,select,textarea,button';
  const forms = [...document.querySelectorAll('form')].map(f => ({
    id: f.id, cl: cls(f).slice(0, 40), act: f.action, m: f.method,
    f: [...f.querySelectorAll(sel)].map(pick)
  }));
  // Outside any <form>: consent-banner buttons, and the stray
  // g-recaptcha-response textarea an invisible reCAPTCHA leaves behind.
  const loose = [...document.querySelectorAll(sel)]
      .filter(e => !e.closest('form')).map(pick);
  const cap = [...document.querySelectorAll('script[src]')].map(s => s.src)
      .filter(s => /recaptcha|hcaptcha|turnstile|challenges\.cloudflare|botdetect|friendlycaptcha|arkose/i.test(s));
  // Separate from cap: the element can exist before its script has fetched.
  const widget = [...document.querySelectorAll(
        '.g-recaptcha,[data-sitekey],.cf-turnstile,.h-captcha,[class*="recaptcha"]')]
      .map(e => cls(e).slice(0, 40));
  const links = [...document.querySelectorAll('a')]
      .map(a => a.innerText.trim().replace(/\s+/g, ' ').slice(0, 45) + ' :: ' + a.href)
      .filter(s => /do not sell|opt.?out|privacy choice|remove|request|dsar|suppress/i.test(s))
      .slice(0, 12);
  return {forms, loose: loose.slice(0, 25), cap, widget, links,
          title: document.title, len: document.body.innerText.length,
          txt: document.body.innerText.replace(/\n{2,}/g, '\n').slice(0, 900)};
}
"""

# A stock headless UA string is itself a bot signal on some broker sites, and
# the point of this tool is to see what an ordinary visitor sees.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def probe_one(url, headed=False):
    """Render ONE url and return its description. Runs in a child process."""
    rec = {"url": url}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        if True:  # indentation kept so the body below reads unchanged
            try:
                resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=30000)
                rec["status"] = resp.status if resp else None
                rec["final"] = page.url
                # Consent portals and SPAs need a beat after DOMContentLoaded
                # before their form exists to be read.
                page.wait_for_timeout(2500)
                rec.update(page.evaluate(JS))
                # Same-origin policy makes this the ONLY way to see a form
                # that a broker embeds rather than serves. hireright.com's
                # consumer-rights request page is the case that forced it:
                # the page renders, is titled as the request form, and
                # document.querySelectorAll('form') on the main frame
                # returns NOTHING, because the form is in a child frame.
                # Reported separately from `forms` so that "the page has no
                # form" and "the page's form is embedded" never read alike.
                frames = []
                for fr in page.frames:
                    if fr is page.main_frame:
                        continue
                    try:
                        got = fr.evaluate(JS)
                    except Exception:  # cross-origin, or the frame went away
                        frames.append({"url": fr.url, "unreadable": True})
                        continue
                    # An ad iframe or a tracking pixel has no controls and
                    # is only noise in the output.
                    if got["forms"] or got["loose"] or got["cap"]:
                        got["url"] = fr.url
                        frames.append(got)
                if frames:
                    rec["frames"] = frames
            except Exception as exc:  # noqa: BLE001 -- the failure IS the finding
                rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        browser.close()
    return rec


def probe(targets, headed=False, out_path=None):
    """Render each (broker_id, url) and return {broker_id: description}.

    EACH TARGET RUNS IN ITS OWN CHILD PROCESS, killed if it overruns
    ``TARGET_BUDGET_S``. That costs a browser launch per target and is worth
    it: a wedged child frame can hang ``frame.evaluate`` forever, and a hang
    inside Playwright's sync API cannot be interrupted from within the same
    process. Signals were tried first and are WORSE THAN USELESS here -- a
    SIGALRM raised into Playwright's greenlet unwound it silently, ending the
    run early with exit code 0 and no error anywhere. A killed child cannot
    lie about it.

    ``out_path``, if given, is rewritten after EVERY target rather than once
    at the end, so an abandoned run keeps everything it had finished.
    """
    results = {}
    for broker_id, url in targets:
        cmd = [sys.executable, os.path.abspath(__file__), "--one", url]
        if headed:
            cmd.append("--head")
        try:
            done = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=TARGET_BUDGET_S)
            rec = json.loads(done.stdout) if done.stdout.strip() else {
                "url": url,
                "error": "child produced no output: %s" % done.stderr[-200:]}
        except subprocess.TimeoutExpired:
            # The finding, not a failure of the sweep: some frame on this
            # page never yields. Worth retrying alone before reading anything
            # into it, since a slow network looks identical from here.
            rec = {"url": url, "timed_out": True,
                   "error": "TargetTimeout: exceeded %ds" % TARGET_BUDGET_S}
        results[broker_id] = rec
        if out_path:
            json.dump(results, open(out_path, "w"), indent=0)
        print("probed %-28s %s %s" % (broker_id, rec.get("status"),
                                      rec.get("error", "")),
              file=sys.stderr, flush=True)
    return results


def main(argv):
    if "--one" in argv:
        rec = probe_one(argv[argv.index("--one") + 1], headed="--head" in argv)
        json.dump(rec, sys.stdout)
        return 0
    if len(argv) < 3:
        print(__doc__.strip().split("USAGE")[-1], file=sys.stderr)
        return 2
    targets = json.load(open(argv[1]))
    out = probe(targets, headed="--head" in argv, out_path=argv[2])
    json.dump(out, open(argv[2], "w"), indent=0)
    print("wrote %s: %d targets" % (argv[2], len(out)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
