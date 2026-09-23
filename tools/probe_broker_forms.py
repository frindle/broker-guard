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

READ-ONLY, ALWAYS
-----------------
Nothing is ever typed, clicked or submitted. The browser navigates, waits for
the page to settle, reads the DOM and closes. A broker's opt-out form must
never be submitted by a reconnaissance pass.

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
import sys

from playwright.sync_api import sync_playwright

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


def probe(targets, headed=False):
    """Render each (broker_id, url) and return {broker_id: description}."""
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(user_agent=UA)
        for broker_id, url in targets:
            page = ctx.new_page()
            rec = {"url": url}
            try:
                resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=30000)
                rec["status"] = resp.status if resp else None
                rec["final"] = page.url
                # Consent portals and SPAs need a beat after DOMContentLoaded
                # before their form exists to be read.
                page.wait_for_timeout(2500)
                rec.update(page.evaluate(JS))
            except Exception as exc:  # noqa: BLE001 -- the failure IS the finding
                rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            page.close()
            results[broker_id] = rec
            print("probed %-28s %s %s" % (broker_id, rec.get("status"),
                                          rec.get("error", "")),
                  file=sys.stderr)
        browser.close()
    return results


def main(argv):
    if len(argv) < 3:
        print(__doc__.strip().split("USAGE")[-1], file=sys.stderr)
        return 2
    targets = json.load(open(argv[1]))
    out = probe(targets, headed="--head" in argv)
    json.dump(out, open(argv[2], "w"), indent=0)
    print("wrote %s: %d targets" % (argv[2], len(out)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
