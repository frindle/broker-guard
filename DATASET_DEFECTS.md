# Dataset and surface defects found by the mapping sweep

GENERATED FILE -- do not edit by hand. Regenerate with
`python3 gen_dataset_defects.py`. Every quoted paragraph below is
verbatim from a broker's entry in `broker_guard/search_forms.py` or
`broker_guard/optout_forms.py`; fix the entry, not this file.

## What this is for

The sweep flags defects but deliberately DOES NOT fix
`data/source-brokers.json` -- a mapping pass that also edits its own
input cannot be audited afterwards. This file consolidates those flags
so they can be resolved as one piece of research (verify which company a
mismatched form really belongs to, recheck ownership on parked domains,
find the live URL behind a dead one) instead of staying scattered across
hundreds of entries.

Two scopes are mixed here on purpose, and they need different remedies:

* **dataset** -- the row in `source-brokers.json` is wrong or stale.
  Fixable by editing the dataset.
* **broker-surface** -- the broker's own published rights channel is
  broken (a dead link, an unconfigured template, a form asking the wrong
  question). NOT fixable from here; worth recording because it affects
  whether a person can exercise a right at all, and may be worth
  reporting to the broker or a regulator.
* **unreachable** -- the company or its site cannot be reached at all:
  parked or for-sale domains, suspended hosting, expired certificates,
  sites that never launched, companies that have closed. These need
  RECHECKING rather than fixing, and they are the most time-sensitive
  rows here. A parked domain is the sharpest case: if it is bought, the
  recorded opt-out path could later belong to a stranger, so a recheck
  must confirm OWNERSHIP and not merely that a page has appeared.

## Caveats that matter before acting on this

Findings are extracted from English prose by keyword, so this list is
**not complete** -- an entry that describes a defect in other words is
missed. The `kind:` tag is a keyword guess, not a verified
classification: read the quoted paragraph before trusting it. Nothing
here has been re-verified since the date stated inside each quote, and
several of these defects are the kind that resolve themselves (an
expired certificate gets renewed, a suspended host comes back).

## Summary

71 findings across 58 brokers.

| scope | findings |
| --- | --- |
| broker-surface | 5 |
| dataset | 32 |
| unreachable | 34 |

| kind (keyword guess) | findings |
| --- | --- |
| parked-or-defunct | 35 |
| dead-url | 11 |
| entity-mismatch | 7 |
| unclassified | 7 |
| broker-surface-defect | 5 |
| rebrand-or-domain-change | 4 |
| contact-address-oddity | 1 |
| stale-200 | 1 |

## Findings by broker

### `acuityads-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and there are three separate reasons to leave it open. FIRST, A REBRAND THE DATASET DOES NOT RECORD: privacy.acuityads.com resolves through to illumin.com. AcuityAds now trades as illumin, and every live URL is on the new domain. Flagged, not fixed. SECOND, the form is real but barely legible from the outside. illumin.com/opt-out/ hosts a HubSpot form inside an ABOUT:BLANK frame -- injected by script rather than served from a URL -- containing a single required input[name='email'] with a per-render id (email-56f7074b-6a42-488a-abca-2066a9726da2) and a Submit. No captcha script was seen, but the frame is about:blank, so that observation covers the parent page and NOT reliably the frame's own contents. Under this module's standing rule that is not a finding of 'no captcha'. A recipe would also have to address a frame with no URL and an id that changes per render -- neither impossible nor stable. THIRD, AND THE BEST FINDING HERE: the consent page carries UNREPLACED COOKIEBOT TEMPLATE PLACEHOLDERS. Its links include a literal '[#DSR_FORM_URL_TEXT#]' pointing at 'illumin.com/opt-out-success/[#DSR_FORM_URL#]', alongside '[#IABV2SETTINGS#]'. The DSR form URL -- the data subject request link, the thing a person on a privacy page is looking for -- was never configured, so the banner offers a link to a page that cannot exist. That is a real, checkable defect in the broker's published rights channel, not a rendering artifact. itops@acuityads.com is the dataset contact; note it is an operations address, not a privacy one.

### `agrgroupinc-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-23: the domain does not resolve. Both agrgroupinc.com and www.agrgroupinc.com fail DNS with getaddrinfo ENOTFOUND, so no page of this broker exists to carry a search surface. The company is real -- CA data-broker registration 186616, All Global Resources, LLC, Henderson NV, privacy@agrgroupinc.com -- but it is a registration with no live website, which is also why the dataset lists it as email-only. If the domain ever comes back this call should be revisited.

### `assurance-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23: assurance.com serves an EXPIRED TLS CERTIFICATE on both the homepage and the dataset's privacy- practices URL, so no page was rendered on either leg. Assurance IQ is a Prudential-owned insurance-quoting marketplace, which would ordinarily point at no-surface, but nothing was observed. Note this is the second row in this batch behind an expired certificate (see take5mg-com, which turned out to be a parked domain) -- so check first whether assurance.com is still a live business or a lapsed one.

### `backgroundchecks-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > FCRA CATEGORY, verified 2026-09-23, and folded into the shared background-screening treatment rather than given a recipe. backgroundchecks.com sells pre-employment screening reports to employers. The dataset's /privacy path returns a genuine HTTP 404 ('This is a 404 error, meaning this link doesn't exist'), and the only frame on the page is a HubSpot chat widget. Flagged as a dataset defect; not fixed. More to the point, the routes the site does offer are the FCRA ones, visible in its own navigation: 'Get a Copy of Your Background Report' and 'Dispute Background Report'. Those are the statutory file-disclosure and dispute rights against a consumer reporting agency, and they are not opt-outs. A CRA regulated under the FCRA cannot simply delete a person from its files on request the way a marketing list broker can -- which is why this module treats the whole category as no-surface with an explanation rather than as a broker refusing to cooperate. The distinction to preserve for anyone reading this row: unlike g2risksolutions-com, which is a FURNISHER feeding data into TransUnion's files, backgroundchecks.com compiles and issues reports itself. Both are FCRA entities; only the latter holds a file a person can demand a copy of. support@backgroundchecks.com is published and is the right channel for a disclosure or dispute request.

### `bidr-io`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: the page could not be reached at all, and the failure is specific enough to be worth recording. A headless Chromium navigation to the dataset's opt_out_url (optout.prod.bidr.io/optout) failed with net::ERR_CERT_COMMON_NAME_INVALID -- the TLS certificate served on that host does not cover that name, so no browser will load it without an explicit override, which this tool will not do. That is a misconfiguration on Beeswax's side rather than an anti-bot wall, and it means the opt-out is effectively unavailable to any ordinary consumer using an ordinary browser -- which is itself the finding. Next pass: recheck whether the certificate has been fixed; if it has not, this row arguably belongs under NO_OPTOUT_SURFACE, because an opt-out nobody can open is not an opt-out.

### `blisspointmedia-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, for a network reason rather than a research one: www.blisspointmedia.com failed to RESOLVE (net::ERR_NAME_NOT_RESOLVED) from a headless Chromium on this host, so no page was reached. This is the second row in the dataset to fail this way (see nuwber-com), and the two should be rechecked together from a different network before either is called dead -- a DNS failure is not a 404 and is not an anti-bot block. If it does turn out to be gone, note that Bliss Point Media was acquired and may now trade under another name, the same trap corelogic-com fell into.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23: www.blisspointmedia.com did not RESOLVE (net::ERR_NAME_NOT_RESOLVED) from a headless Chromium on this host, so nothing about either leg can be stated. Recheck from a different network before calling it dead, and check whether the company now trades under another name. Same situation as nuwber-com; see the opt-out leg's entry.

### `bridgevine-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Reachability failure, 2026-09-23 -- same finding as the search leg. https://bridgevine.com/ does not resolve (net::ERR_NAME_NOT_RESOLVED from a real browser), so no opt-out page could be reached. Recorded as undecided rather than absent because one network's DNS failure is not proof of a dead company; recheck from elsewhere. Dataset contact dwayne.landry@bridgevine.com is a personal address, not a privacy alias, and would be worth verifying before anyone relies on it.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > Reachability failure, 2026-09-23: https://bridgevine.com/ does not resolve (net::ERR_NAME_NOT_RESOLVED from a real browser). Recorded as UNDECIDED rather than no-surface because a DNS failure from one network is not proof the company is gone -- it should be rechecked from a different network before anyone concludes the domain is dead. The dataset carries an opt_out_email for it (dwayne.landry@bridgevine.com), which is a personal address rather than a privacy alias and is itself worth doubting.

### `brightswipe-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Reachability failure, 2026-09-23 -- same finding as the search leg. https://brightswipe.com/ does not resolve (net::ERR_NAME_NOT_RESOLVED). Recheck from another network before concluding the domain is retired. Dataset contact: admin@brightswipe.com.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > Reachability failure, 2026-09-23: https://brightswipe.com/ does not resolve (net::ERR_NAME_NOT_RESOLVED). Same caveat as bridgevine-com -- recheck from another network before calling the domain dead. Dataset contact: admin@brightswipe.com.

### `calltruth-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Reachability failure, 2026-09-23 -- same finding as the search leg. https://www.calltruth.com/opt_out.php does not resolve (net::ERR_NAME_NOT_RESOLVED), so no opt-out page could be reached. Recheck from another network before concluding the domain is retired; note the URL shape (/opt_out.php) suggests the surface did once exist. The dataset records no email for this row, so it has no working channel at all.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > Reachability failure, 2026-09-23: https://www.calltruth.com/opt_out.php does not resolve (net::ERR_NAME_NOT_RESOLVED from a real browser). Same treatment as the bridgevine/brightswipe rows -- a DNS failure on one network is not proof the domain is retired, so this is undecided pending a recheck from elsewhere. The dataset holds no email for this row, so it currently has no working channel at all.

### `cardlytics-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified by browser render 2026-09-23, and this row is also a dataset defect. The recorded opt_out_url, datagrail.cardlytics.com, returns a hard HTTP 404 ('Page not found') -- the DataGrail portal is gone, not merely JS-rendered. www.cardlytics.com/privacy-notice and /privacy also 404; the live policy is www.cardlytics.com/privacy-policy, which was read in full (about 50k characters) and offers NO web form of any kind: every rights path it names is a mailbox. Its California, Colorado and Connecticut sections each say rights are exercised 'by emailing us at privacy@cardlytics.com', and appeals go to the same address with the subject 'Appeal of Consumer Rights Request'. Separately, the policy says opting out of a card- linked marketing program is done through the Publishing Partner (the consumer's own bank), not through Cardlytics. Mailbox-only with no webform is the definition of this bucket. Note the dataset carries legalnotices@cardlytics.com while the policy names privacy@cardlytics.com -- the dataset should be corrected on both the dead URL and the address.

### `carmarketsolutions-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Reachability failure, 2026-09-23 -- same finding as the search leg, and a different failure mode from the two ERR_NAME_NOT_RESOLVED rows beside it: https://carmarketsolutions.com/ answers DNS but never completes a page load, timing out at 30s before DOMContentLoaded. That points at a hung or firewalled host rather than a retired domain, so it is worth a retry later and from another network. The dataset records no email for this row, so it presently has no working channel of any kind.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > Reachability failure, 2026-09-23: https://carmarketsolutions.com/ resolves but never completes a connection -- the browser timed out after 30s without reaching DOMContentLoaded. Distinct from the two ERR_NAME_NOT_RESOLVED rows in this batch: something answers DNS here, so this is more likely a hung or firewalled host than a retired domain. Recheck later and from another network. The dataset holds no email for this row, so it currently has no working channel at all.

### `catalyzeai-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, verified 2026-09-23: the row's opt_out_url https://www.catalyzeai.com/opt-out returns HTTP 404 ('Page not found. The page you are looking for doesn't exist or has been moved.'), and the dataset's own notes already record that the broker's email bounced on 2026-08-20. That leaves this row with NO working contact of any kind. Not fixed in data/source- brokers.json, only recorded here. What is left to try: find a current privacy policy on catalyzeai.com and read the rights section off it, or establish that the company has folded or been absorbed -- either answer resolves the row, and neither was established today.

### `cdkglobal-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified by browser render 2026-09-23: CDK's 'Do Not Sell or Share My Personal Information' footer link has an EMPTY href -- it is a JavaScript consent-widget trigger, not a page -- and the privacy statement scopes it explicitly to tracking technologies: 'You may manage your preferences on the tracking technologies deployed on the Site by clicking on the Do Not Sell or Share My Personal Information link at the footer'. Its own text says 'We do not sell your Personal Information' for other purposes, and every actual rights request (deletion, access) is directed to a contact address rather than a form. So there is no web surface that suppresses a person's records here, only a cookie preference for this website's visitors. The remaining channel is mailbox-only, which this codebase cannot represent. Dataset contact for a human: james.kinzer@cdk.com.

### `civisanalytics-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, verified 2026-09-23: the row's opt_out_url https://www.civisanalytics.com/privacy-policy/supplemental- privacy-notice/ returns HTTP 404 ('The page you are looking for doesn't exist'), serving only a cookie banner. This is a circular dead end, because the dataset notes record that dataprotection@civisanalytics.com replied that they cannot process requests until THIS form is completed -- the form they point at no longer exists. Not fixed in data/source- brokers.json, only recorded here. Next step for a researcher: locate the supplemental notice at its current path under civisanalytics.com/privacy-policy and quote the real request surface back to that mailbox.

### `completemailinglists-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, verified 2026-09-23: the row's opt_out_url https://www.completemailinglists.com/node/3697 returns HTTP 404. What is behind it is worth recording: the 404 page is a half- finished template whose navigation still reads 'Menu Item One / Menu Item Two / Menu Item Three', so the site appears to have been rebuilt without its rights pages being carried across. Its sibling completemedicallists.com DOES publish a working CCPA form at /ccpa.php, so the obvious next step is to check whether completemailinglists.com serves the same /ccpa.php form -- if it does, this row resolves immediately. Not fixed in data/source- brokers.json. Dataset contact: ewoolf@completemailinglists.com.

### `contacts411-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: www.contacts411.com does not resolve -- net::ERR_NAME_NOT_RESOLVED, i.e. DNS returns nothing for the host, so there is no server to ask. That is a stronger signal than a timeout or a 404 (both of which need a live host) and is consistent with the domain being gone rather than the page having moved. Still recorded as undecided rather than no-surface, because 'the domain no longer resolves' is a claim about the company's continued existence, and that is a research question for the consolidated defects list, not something a single failed lookup settles. UNREACHABLE for the defects list.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: the host does not resolve in DNS (ERR_NAME_NOT_RESOLVED). The name suggests a consumer directory, which is exactly why no surface is being inferred from it.

### `data-axle-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified by browser render 2026-09-23: the single best-specified opt-out form in this batch, and walled. The recorded /do-not- sell-my-data/ leads to /privacy-rights-request/, whose Gravity Form #gform_4 carries input_1 First Name, input_3 Last Name, an address group (input_14.1 Street, 14.2 Line 2, 14.3 City, 14.4 a full 50-state-plus-territories select, 14.5 ZIP), input_9 Email, input_10 Phone -- all required -- a required 'Privacy Choice' select whose first option is 'Request to opt out of sale', a free-text detail box, an acknowledgement checkbox (input_13.1) and #gform_submit_button_4. It also carries a HONEYPOT: input_19, labelled 'Instagram', off-layout, which would have to go in forbidden_selectors. What blocks it is a CLOUDFLARE TURNSTILE (challenges.cloudflare.com plus a live .cf-turnstile element). Note the Gravity Forms input_NN names are per-form- build identifiers, so a recipe would be pinned to this revision even if the wall came down. DATASET NOTE: the row's opt_out_email is doba_privacy@donorbase.com, a different brand, which may mean several Data Axle brands were collapsed into one row.

### `datadelivers-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, verified 2026-09-23: the row's opt_out_url https://datadelivers.com/unsubscribe/ returns HTTP 404 ('Page not found'), leaving only the site's WordPress search form. Not fixed in data/source-brokers.json. Worth noting the URL shape: /unsubscribe/ suggests the recorded surface was an email unsubscribe rather than a data suppression, so even if it were restored it may be the wrong request type -- the trap already documented on several consent-portal rows. Next step: look for a privacy or do-not-sell page under datadelivers.com and establish which request types it accepts. Dataset contact: supplier@datadelivers.com, which is addressed to data SUPPLIERS rather than consumers and is itself suspect.

### `datalinedata-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, verified 2026-09-23: the row's opt_out_url https://datalinedata.com/privacy-portal/ returns HTTP 404, so the recorded surface is gone. One genuinely useful detail came out of the render anyway: the site's reCAPTCHA Enterprise is BROKEN -- its challenge frame reports 'This site is exceeding reCAPTCHA Enterprise free quota' -- which means any form on this domain may be unsubmittable for everyone right now, not just for this tool. Anyone returning here should check that before concluding a form is walled against them specifically. Next step: find the live privacy portal (the footer offers only 'Request a Demo'). Dataset contact: psobel@datalinedata.com, a personal address.

### `degree-me`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23: the domain serves nothing. degree.me has NS delegation to AWS Route53 (ns-1086.awsdns-07.org, ns-1685.awsdns-18.co.uk, ns-236.awsdns-29.com, ns-918.awsdns-50.net) but NO A or AAAA record resolves, confirmed by dig both ways, and curl returns http_code 000. This looks like a dormant registration tied to ACE Agents Inc. / academixdirect.com. It sits here rather than under NO_SEARCH_SURFACE because a domain that does not resolve today may resolve tomorrow; recheck resolution before deciding.

### `dynata-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23, confirming and extending the research already in the dataset's own note rather than repeating it. Rendering dynata.com and dynata.com/privacy (which resolves to /privacy/) found exactly one form on each page: the WordPress site search. No request-form anchor exists on either, matching the note's finding that the 'Do Not Sell My Information' control is a Usercentrics-rendered JavaScript pop-up with no linkable address. It is recorded as blocked rather than as having no surface because a surface does exist -- it simply cannot be reached or completed. Per the dataset note, the pop-up asks first name, last name and email, then presents A CAPTCHA, and then requires clicking a 'Confirm Data Request' link sent by email. Two walls in series: the challenge, and an out-of-band confirmation hop. Even setting the captcha aside, there is no stable URL for a recipe to navigate to. privacy@dynata.com and (833) 909-1804 / 833-681-0436 are the channels the published policy names, and email is the only automatable one.

### `electroniccommerceatoz-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: electroniccommerceatoz.com does not resolve. The navigation failed with net::ERR_NAME_NOT_RESOLVED, i.e. DNS returned nothing -- not a refused connection, not a timeout, not a certificate mismatch. Nothing about the broker can be said from that. It is recorded as undecided rather than as having no surface because a single DNS failure from one network is weak evidence. This sweep has already accumulated a short list of rows failing the same way (nuwber, bridgevine, brightswipe, carmarketsolutions, calltruth, blisspointmedia) and they should be rechecked together from a different resolver before any of them is written off -- a local resolver, a captive network or an upstream block would produce exactly this result for a domain that is perfectly alive. The dataset records no opt-out email and an opt_out_method of 'unknown' for this row, so if the domain really is dead there may be no channel at all, which is itself worth establishing rather than assuming.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-23 by rendering the site. No consumer-facing lookup exists here. electroniccommerceatoz.com does not resolve in DNS, so neither leg has a surface to describe; see the opt- out entry for the recheck that is owed. The leg is closed rather than left open, because a search recipe could never be written against a surface the broker does not offer.

### `emailindustries-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: https://www.emailindustries.com/legal/ did not finish loading within 75s (navigation timeout), so nothing was rendered to read. Not recorded as blocked -- a timeout is not a refusal, and there was no challenge page, no 403 and no captcha, just no response in time. Retry before drawing any conclusion.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Recorded 2026-09-23 on WEAKER evidence than the others in this group, and flagged as such: the site timed out and was never read. Placed here on the strength of the company's name and category alone -- email-deliverability tooling sold to senders. If that inference matters to anything downstream, re-probe; the opt-out leg is separately undecided for the same timeout.

### `emerges-com`

- **scope:** dataset | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-23, and this is the first row in the sweep where the right answer is that THE BROKER HAS LEFT THE BUSINESS. emerges.com still serves a site advertising 'watercraft, aircraft, voter and snowmobile registrations with pilot, hunting and fishing licenses', and its nav still carries 'REMOVE ME/OPT OUT' pointing at a Google Form. Following that form to its full address (docs.google.com/forms/d/e/1FAIpQLSdi3KjEPMsVnXQL- KllxvgOQWxvLpLfuz30-Z_eqXDHGEbX6w) redirects to /closedform, which says, in the broker's own words: 'eMerges Opt Out is now Disabled. *As of July 1, 2025 eMerges ceased operating as a List Broker. 1) eMerges is not acquiring, processing, publishing or selling any lists either directly or indirectly and including but not limited to government records. 2) eMerges has ceased operating its entire list business therefore this opt out resource has been disabled effective 20260223.' So there is no surface, and uniquely there is nothing that a surface would accomplish. Recorded as no-surface rather than blocked or undecided because the absence is deliberate, dated, and explained by the broker. DATASET NOTE, flagged and not acted on: this row is arguably retired rather than mapped, and a defunct broker in a 969-row checklist is worth distinguishing from a live one with no form. That is a decision about the dataset's shape, not about this broker, so it is left to a human. Note also, in passing, that the surviving evidence is a Google Form -- the same pattern as clay-com, which is still open pending aria-labelledby resolution.

### `experian-com`

- **scope:** dataset | **kind:** rebrand-or-domain-change | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Verified by browser render 2026-09-23: the dataset opt_out_url www.experian.com/privacy/opting_out is a rights EXPLAINER, not a request surface -- the only form on the rendered page is the site-wide business search (input[name=q]). An invisible reCAPTCHA is live on it anyway (recaptcha__en.js plus a .grecaptcha-badge and a g-recaptcha-response textarea outside any form), which is the second time this sweep that a static fetch would have reported a captcha-free page. Experian also runs several DIFFERENT consumer channels that are easy to confuse and were not resolved here: the FCRA prescreen opt-out (optoutprescreen.com, itself blocked -- see its own entry), a marketing-mail opt-out, and a CCPA/state-rights portal. A future researcher needs to establish WHICH surface actually suppresses Experian Marketing Services data (including the acquired Tapad identity graph, which the dataset notes were merged into this row) and whether it can be reached without an identity-verified login, since the credit-file side certainly cannot. Second channel for a human: privacy@experian.com.

### `exploreatlas-io`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23. www.exploreatlas.io/privacy returns HTTP 200 whose body reads 'This page couldn't be found. You may not have access, or it might have been deleted or moved.' That phrasing is a Notion or Super-style hosted-site message rather than a web-server 404, so the page was published at some point and has since been unpublished, deleted or made private. Recorded as undecided rather than no-surface for two reasons. The site itself was not examined beyond the recorded privacy path, so a rights page may exist elsewhere on the domain. And the row has a bigger question hanging over it than a missing page. DATASET DEFECT, flagged not fixed: the row's domain is exploreatlas.io but its contact is SCOTT@HUNTCLUB.COM -- a personal address at an unrelated company. Hunt Club sells recruiting services; Atlas is a separate product name. Either the row conflates two companies, or Atlas is a Hunt Club property and the dataset records the parent's contact without saying so. As with the forms.gle row in the previous batch, acting on it risks sending a person's opt-out to a company that holds nothing about them. Establishing which company this row is about is the first step, before any further page-hunting.

### `flashintel-ai`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. The dataset's flashintel.ai/dont-sell-my- information redirects to www.FLASHLABS.ai/dont-sell-my- information -- a rebrand the dataset does not record, flagged here and left unfixed. The dataset's contact for the row, legal@myflashcloud.com, is a third distinct name again. The form is real: Full Name, Current Company, Profile URL and Business Email, all four required, under a 'Submit Request' button. Blocked by reCAPTCHA -- a g-recaptcha-response textarea inside the form with both api2/anchor and api2/bframe frames attached. Worth recording even past the captcha, because it is a second, independent obstacle of a kind this sweep keeps meeting: the form asks for PROFILE URL and CURRENT COMPANY, both required. This is a B2B contact enrichment product, so the record it holds is keyed to a professional profile rather than to a household. resolve_fields has no source for either, and a profile URL is not something the tool could infer -- the user would have to supply it. Same shape as the MAID-keyed brokers (complementics, collectivedata, datafy, factori): the identifier the broker files you under is not one this codebase collects.

### `force-com`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. Two layers of confusion resolved, then a wall. DATASET DEFECT, flagged and NOT fixed: the row is e.Republic (a government-and-education media company) but is KEYED to force.com, which is Salesforce's hosting domain rather than anything e.Republic owns. The broker_id 'force-com' is therefore meaningless, and any future row hosted on Salesforce would collide with it. The recorded URL (erepublic.secure.force.com/PrivacyRequest/) is dead: Salesforce answers 'URL No Longer Exists'. So does erepublic.com/privacy- policy/. The live surface was found on the footer of e.Republic's own 404 page -- erepublic.my.salesforce- sites.com/PrivacyRequest/ -- i.e. the same app migrated from the retired *.secure.force.com hostname to the current *.my.salesforce-sites.com one. That form is genuine (a request- type select, name, phone, email, full address, a state select, a comments box and a declaration 'under penalty of...' checkbox) and is blocked three times over: * reCAPTCHA, via a hidden recaptchaToken input. * A HONEYPOT named almost plausibly: a hidden text input ending ':HomeAddressHP' -- the HP suffix being the only giveaway on a form that also asks for a real home address. * A TIMING TRAP, which is new in this sweep and worth naming: the form carries formLoadTime and TimeSpent inputs, so the server judges HOW LONG the form took to fill. A recipe that fills instantly is detectable even with every field correct and every honeypot avoided. And even past all three, the field names are Visualforce's positional auto-ids -- j_id0:j_id2:j_id3:j_id31:j_id36 and so on -- which renumber whenever the page is edited. This is the most fragile naming scheme the sweep has met, worse than Gravity's input_N. privacy@erepublic.com is the published channel.

### `forms-gle`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and this row has TWO dataset defects stacked on each other. Both are flagged here and neither is fixed. FIRST: the row's domain is 'forms.gle'. That is Google's URL shortener, not a broker. Slugged, it becomes the key forms-gle, which identifies no company and will collide with any other row whose opt-out happens to be a Google Form. The row's real subject is whatever company the form belongs to. SECOND, and worse: the form does not belong to the company the row names. The dataset's contact is privacy@REALEFLOW.com. The form at forms.gle/S7vW6zXPwgtnZ9ZF9 is titled 'GROWBOTS OPT-OUT REQUEST FORM' and its text is Growbots' throughout -- 'we will remove the profile and business information linked to this email from our database'. Realeflow sells real-estate investor leads; Growbots sells B2B sales prospecting. They are unrelated. Either the URL was copied into the wrong row or the email was, and there is no way to tell which from here. Acting on it would send a person's opt-out to a company that may hold nothing about them, while leaving the company that does hold something untouched. That alone makes it unsafe to automate. For the record, the Growbots form itself is a Google Form (mG61Hd, entry.1596228221) with a single required text input and the usual hidden fvv / fbzx / pageHistory / submissionTimestamp apparatus. Its labels are carried by aria-labelledby rather than by label elements, the same gap already recorded for clay-com and factori-ai -- the probe cannot read the question text, so which field is which is inferred, not observed. The form also states an out-of-band hop: 'upon filing one and CONFIRMING YOUR EMAIL, we will remove the profile'. Resolving this row starts with establishing which company it is actually about.

### `fullcontact-com`

- **scope:** dataset | **kind:** rebrand-or-domain-change | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. platform.fullcontact.com/your-privacy- choices is a multi-step rights wizard -- five buttons outside any form (Access My Data, Correct My Data, Do Not Sell or Share, Limit Sharing Of My Data, Delete My Data) and no fields until one is chosen. Structurally identical to fideo-ai in the previous batch. Blocked by reCAPTCHA: gstatic's recaptcha script and google.com/recaptcha are both loaded on the landing step, before any field exists, so the challenge is attached to the flow rather than to a particular page. DATASET NOTE, flagged and not fixed: the row's contact is privacy@ziffdavis.com, not a fullcontact.com address. FullContact was acquired and its rights requests now route to Ziff Davis. That is correct rather than wrong -- but it means anyone reconciling this row by domain will think the address is a mistake, and it is worth knowing it is not. The page also names an authorised-agent route by email to privacy@fullcontact.com, which still resolves.

### `fusedleads-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: fusedleads.com could not be loaded at all. The navigation failed with net::ERR_CERT_DATE_INVALID -- the site's TLS certificate is expired or not yet valid. That is worth distinguishing carefully from the other failure modes in this module. It is NOT a DNS failure (the name resolved), NOT a refused connection (the handshake got far enough to present a certificate), and NOT an anti-bot wall (a wall serves a challenge page, which reads fine). The host is up and answering; its certificate is simply out of date. Every ordinary visitor is seeing the same browser interstitial, so this is a broker whose site is effectively unreachable to the public rather than one defending itself against automation. It is undecided rather than closed because certificates get renewed, often within days, and the site behind it is unexamined. A retry in a week is the whole next step. If it is still expired then, that is worth saying out loud in any escalation: a data broker whose opt-out channel is unreachable because it has not renewed a certificate is not offering one. The dataset records this row as email- method with greg@fusedleads.com, which at least does not depend on the website.

### `granitelists-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: granitelists.com returns HTTP 403 with the page reading 'Account Suspended. This Account has been suspended. Contact your hosting provider for more information.' That is the hosting provider's own interstitial, not the broker's site. Distinguish this carefully from the other unreachable rows. It is not a bot wall (403 here is the host refusing to serve anyone), not DNS, and not a certificate problem as with fusedleads-com. The account behind the domain has been suspended, most often for non-payment or a terms violation. What makes it consequential rather than merely inconvenient: the dataset records NO opt-out URL, NO opt-out email and an opt_out_method of 'unknown' for this row. So there is no fallback channel to fall back to. If the company still holds data, there is at present no way whatsoever for a person to reach it. Undecided rather than closed because suspensions are reversible and the site behind it has never been seen. Recheck in a few weeks; if it is still suspended and still has no published address, that combination is worth escalating rather than filing.

### `greatlakeslists-com`

- **scope:** dataset | **kind:** stale-200 | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23, and the first finding is a DATASET DEFECT, flagged and deliberately not fixed. The recorded opt_out_url, greatlakeslists.com/opt_out_request.php, still returns HTTP 200 but no longer contains a form -- it renders the site's generic chrome and nothing else. A tool following the dataset would find an apparently healthy page with nothing on it and could easily record 'no surface'. The live surfaces are reached only from the footer: /do-not-sell-ca ('California Consumer Privacy Act Requests') and /do-not-sell-non-ca ('Opt Out Requests Web Form'). A 200 that quietly stopped being the page it used to be is a worse failure than a 404, which would at least announce itself. Both live pages are blocked by reCAPTCHA v3 -- api.js?render= with site key 6LeG5lAbAAAAAB2mnbwCEYHiihwLefn_Udbwksfe, plus the gstatic runtime and an anchor frame, on each. v3 is score-based and entirely invisible: there is no checkbox to click and no puzzle, which means an automated submission is not refused so much as silently scored down. That failure mode is particularly bad for this tool, because the request can appear to go through. Both are also ROLE-GATED wizards before any fields appear: 'Who is submitting this request? ... I am the person opting out / I am an authorized agent'. No field set was reached, so nothing beyond the choice step is recorded. The pages do offer a 'Check the status of your opt-out request' route, which is unusual and useful, and the dataset records no opt-out email for this row, so the web form is the only channel.

### `grin-co`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. grin.co/data-privacy-form/ returns HTTP 200 but the body is a Cloudflare interstitial -- title 'One moment, please...', text 'Please wait while your request is being verified...'. No form, no fields, nothing else in the DOM. This is the shape that most deserves care in this module, because it lies twice. The status code says success. The page renders without error. A probe that only checked for HTTP 200 and then counted form elements would report 'page loads fine, no opt-out form present' and the row would be closed as no-surface -- a conclusion that is exactly backwards, since the URL is named data-privacy-form and the form is almost certainly sitting behind the challenge. Recorded as blocked rather than undecided because the obstacle is deliberate and persistent: Cloudflare's managed challenge is aimed at precisely the kind of headless automation this tool performs, and waiting longer does not resolve it. The only honest statement about what is behind it is that nothing has been seen. The dataset records no opt-out email for this row, so there is no fallback channel to offer a user. A human with an ordinary browser will pass the challenge without noticing it, so the page is reachable to people and not to this tool -- worth saying plainly if the row is ever surfaced in a report.

### `gumgum-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. GumGum's privacy policy has no form of its own; its 'DO NOT SELL', 'Do not Sell' and 'Exercise Your Rights' links all point to the same OneTrust DSAR webform on privacyportal-cdn.onetrust.com. That form was rendered and read. Blocked by reCAPTCHA -- api.js loaded, a g-recaptcha-response textarea inside the form, and both an anchor and a BFRAME attached, the latter meaning the checkbox-with-puzzle variant. THE WRONG-REQUEST-TYPE PROBLEM, and a clear-cut instance of it. The form is headed 'SUBJECT ACCESS FORM' and its first required question is 'I am a (an)' with the options: Prospective Employee, Client, Employee, Visitor, Other. THERE IS NO OPTION FOR A PERSON WHOSE DATA THE COMPANY COLLECTED THROUGH ADVERTISING -- which is everyone this dataset is concerned with. A consumer opting out of GumGum's ad targeting has never been its employee, client or prospective employee, and calling themselves a 'Visitor' asserts a relationship to gumgum.com that they very likely do not have. This is a generic HR-oriented OneTrust template pressed into service as an advertising-privacy channel, and picking any of its options would mean a recipe choosing a characterisation on the user's behalf. Recorded as blocked on the captcha, which is decisive on its own, but the request-type problem would independently keep it out of STAGED_RECIPES. Remaining fields are conventional: First Name, Last Name, Email, Country and a required Request Details textarea, with a request-type multi-select offering Opt out / Update Data / Info Request / Data Deletion / Object to Processing. Note the policy also links the NAI consumer opt-out, which is not GumGum's surface. talbert@gumgum.com is the dataset contact -- a personal address.

### `healthlinkdimensions-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-23, and closed on a defect in the BROKER'S OWN PAGE rather than in the dataset. /consumerprivacyrights is headed 'Exercise Your Consumer Data Privacy Rights' and instructs: 'please FOLLOW THE LINK BELOW to submit a Right to Opt-Out request or a Right to Know Request.' There is exactly one such link on the page, labelled 'Do Not Sell or Share My Personal Information', and it points to https://healthlinkdimensions.com/consumerprivacyrights -- THE PAGE ITSELF. Following the instruction returns the visitor to the instruction. The page contains no form element of any kind. So the published rights channel is a circular reference: a person doing exactly what the broker tells them to do arrives back where they started, with no indication anything went wrong. This is worth recording as an observed fact rather than smoothed over as 'no form found', because it is the difference between a company that offers no web route and one that appears to offer one and does not. The page does carry the company's postal address (1001 Summit Blvd NE, Suite 1125, Atlanta, GA 30319) and phone (404.250.3900), and the dataset records nlenyszyn@healthlinkdimensions.com -- again a personal rather than a role address. Those are the only working channels. Recorded as no-surface because the absence is established, not merely unobserved; if the self-link is ever repointed at a real form, this row should be reopened.

### `heartbeat-ai`

- **scope:** dataset | **kind:** contact-address-oddity | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. heartbeat.ai/optout returns HTTP 200 whose entire body reads 'Checking for any bots ...' with challenges.cloudflare.com/turnstile loaded. No form, no fields, nothing else in the DOM. Same shape as grin-co in the previous batch and worth the same warning: the status code says success and the page renders without error, so a check that counted form elements behind a 200 would report 'no opt-out form present' -- exactly backwards, since the URL is /optout and the form is behind the challenge. Recorded as blocked rather than undecided because the obstacle is deliberate and aimed precisely at headless automation; waiting does not resolve it. DATASET NOTE, flagged not fixed: the row's domain is heartbeat.ai but its contact is contact@SWORDFISH.ai. That is correct rather than wrong -- Heartbeat and Swordfish are the same operation, and Swordfish's own row may exist separately in this dataset. Worth knowing before anyone 'corrects' the address, and worth checking whether the two rows are duplicates of one broker.

### `hightouch-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Verified by browser render 2026-09-23: preferences.hightouch.com is a DataGrail Privacy Request Center that GATES its form behind two pickers before any request fields exist. The rendered page contains exactly four controls and no <form> at all: #privacy- request-center-country-picker and #privacy-request-center- region-picker (MUI Autocomplete text inputs, each with a keyboard_arrow_down toggle button), pre-filled from geolocation as United States / Nevada. No captcha script and no captcha widget at this stage -- which says nothing about the stage after it, per the standing rule that a bot check absent before the form renders is not a bot check absent. What a future recipe- writer needs: drive both Autocompletes (they are listbox_button- style, not <select>), record the request-type choices and field set that appear afterwards, and re-check for a captcha THEN. The dataset notes are consistent with this being the real surface: legal@hightouch.com auto-replies requiring identity verification through this portal.

### `hivestack-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-23. /opt-out-california-residents/ contains NO form element. What it contains is a list of links to OTHER companies' opt-out pages: undertone.com/opt-out/, thenai.org/opt-out/, Google's analytics opt-out, optout.privacyrights.info, and perion.com/ccpa/ -- Perion being Hivestack's parent. None of those is Hivestack's surface. The NAI and Google links suppress ad targeting across participating networks and do nothing to what Hivestack holds; the Undertone and Perion links belong to sibling companies. Offering any of them would be offering a remedy that does not address the complaint, the same reasoning already applied to fourleafdata- com and gundir-com. A DEFECT ON THE PAGE ITSELF, worth recording: the link labelled 'Your Privacy Choices' -- the one a person would click first, since it is the statutory phrase -- has an EMPTY href. It goes nowhere. Clicking it does nothing at all and gives no error, so a visitor would reasonably assume the page was broken or that they had already opted out. So the only route this company publishes for itself is privacy@hivestack.com, recorded in the dataset. Closed as no- surface because the absence is established rather than merely unobserved.

### `idengine-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-23. idengine.com is a PARKED DOMAIN LISTED FOR SALE. The recorded opt-out path /dnsmpi/ redirects into GoDaddy's aftermarket and returns an Akamai 'Access Denied' for 'http://forsale.godaddy.com/forsale/idengine.com'. There is no site behind the name. This is the most complete form of absence in the module, and distinct from its neighbours: reachdata-com had not launched yet, emerges-com had shut down, granitelists- com is suspended and may return. A domain in a for-sale listing has been given up by its owner, and may shortly belong to someone entirely unrelated. That last point is the reason this is worth more than one line. The dataset records NO opt-out email for this row, so the URL was the only channel -- and if the domain is bought, /dnsmpi/ could later resolve to a live page belonging to a different company. A tool that retried this row mechanically could then submit a person's name and address to a stranger. Any future recheck of parked-domain rows should confirm OWNERSHIP, not merely that a page has appeared.

### `imprintanalytics-io`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: https://imprintanalytics.io/limit-the-use-of-my-sensitive-personal-information/ cannot be loaded at all -- net::ERR_SSL_VERSION_OR_CIPHER_MISMATCH, i.e. the TLS handshake fails before any HTTP request is made. That is a server configuration fault on their side, not a block aimed at us and not a missing page: the URL's own slug is a CCPA right, so the surface was evidently meant to exist. Nothing can be said about its contents. Retry later; if it persists, this belongs in the consolidated defects list as UNREACHABLE rather than as a broker finding.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: TLS handshake fails outright (ERR_SSL_VERSION_OR_CIPHER_MISMATCH), so no HTTP request is ever made. Server misconfiguration on their side.

### `information-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NEXT STEP, concretely: fetch /privacy-rights/ and transcribe whatever it serves. Deliberately not doing that from the stale URL's redirect chain here, because the two buttons post rather than link, so what /privacy-rights/ shows may depend on which button was pressed -- and guessing which is how a recipe ends up filing a copy request when the user asked for deletion. DATASET NOTE: the source row's opt-out URL is wrong.

### `integratedmedicaldata-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: integratedmedicaldata.com does not resolve (net::ERR_NAME_NOT_RESOLVED), so there is no host to ask. UNREACHABLE for the defects list. Given the name, a research pass should establish what became of this company and where any health data it held went -- that matters more than the usual dead domain, and a failed DNS lookup does not answer it.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: the host does not resolve (ERR_NAME_NOT_RESOLVED), so nothing can be said about any surface. UNREACHABLE for the defects list; see optout_forms for why this particular dead domain deserves a research pass.

### `intellicorp-net`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23. The dataset's URL, https://www.intellicorp.net/marketing/Your-California-Privacy-Rights, returns 404. DATASET NOTE: stale opt-out URL. IntelliCorp is a background-screening CRA (a Verisk business), so the FCRA category note above likely governs once a live page is found -- but a 404 establishes nothing and no category is recorded.

- **scope:** dataset | **kind:** unclassified | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23: the dataset's URL 404s and no other path was probed. A background-screening CRA, so any 'search' is a file disclosure gated behind identity verification rather than a public index -- same distinction as innovis-com above. DATASET NOTE: stale URL.

### `nuwber-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and for an unusual reason worth recording rather than retrying blindly: nuwber.com would not RESOLVE. A headless Chromium navigation to the dataset's opt_out_url (nuwber.com/removal/link) failed with net::ERR_NAME_NOT_RESOLVED -- a DNS failure, not a timeout, a certificate problem or an anti-bot block, and distinct from the HTTP errors recorded on other rows here. One observation from one host on one day is not enough to call a large and previously-active people-search site dead, so this is undecided rather than no-surface. Next pass: resolve the name from a different network before concluding anything, and if it resolves, render /removal/link and transcribe. Both legs of this broker are unresolved for the same reason.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23 for a network reason, not a research one: nuwber.com failed to RESOLVE (net::ERR_NAME_NOT_RESOLVED) from a headless Chromium on this host, so no page was ever reached. Nuwber is a well-known people-search site, so a single DNS failure is not grounds for a no-surface call in either leg. Next pass: resolve the name from a different network first. See the opt-out leg's entry, which is unresolved for the same reason.

### `parade-pet`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and the surface was found only because the site leaks it. parade.pet is a single-page app: every path, including ones that return HTTP 404, serves the same shell, and that shell contains every form the app will ever show. Enumerating them turns up signUpForm, loginForm, phoneNumberForm, smsCode, emailCodeForm -- and, decisively, form#optOutLoginForm, an email box with a Login button. So an opt-out flow exists and is reachable, which the visible site never advertises; the homepage FAQ item 'How do I delete my account and remove my ...' links only to an on-page accordion, and an 'Opt out of marketing' link points at '#'. It is undecided because the flow is GATED ON AN EMAILED CODE. optOutLoginForm takes an email and logs you in; emailCodeForm then asks for a code delivered out of band. Nothing beyond that step was observed, so the fields that carry the actual request are unknown. Same shape as forager-ai in the previous batch and as the shipped ADVANCEDBACKGROUNDCHECKS recipe, so not out of scope in principle. One caution for whoever continues: because the SPA serves all forms at all times, PRESENCE OF A FORM IN THE DOM DOES NOT MEAN IT IS ON SCREEN. A recipe here must assert the opt-out view is actually displayed before filling anything, or it will type into a hidden login box and report success. DATASET NOTE, flagged not fixed: this row's domain is parade.pet but its contact is hello@goodboystudios.com -- the operator's name, not the site's.

### `privacycompliance-biz`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23. https://privacycompliance.biz/databaseusa-opt-out-process/ returns a 404 ('Looks like you have taken a wrong turn'); the site itself is up and serves a WordPress 404 template with a working search box, so this is a dead PATH on a live host rather than a dead host. DATASET NOTE: the source row's opt-out URL no longer resolves to a page. Worth a look for a current DatabaseUSA opt-out path before writing this off -- the slug names a specific process that presumably moved rather than vanished.

### `privatereports-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Two readings, not distinguished by this probe: the opt-out path redirects to search, or the opt-out flow BEGINS with a search to locate your record -- which is how several already-mapped brokers work. The second is likelier given the URL's /optOut/ prefix, and it matters because under that reading the automation would be running a search on Penn against a site that then has to be carried through to a removal step. Recording it as undecided rather than guessing. NEXT STEP: submit a search from the opt-out path and observe whether the result page offers a removal action; that single observation resolves this leg. DATASET NOTE: if the first reading is right, the source row's opt-out URL is stale.

### `reachdata-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-23. reachdata.com has not launched. The site is a single placeholder page reading 'Coming Soon!!' over a pitch for a sales-and-recruiting contact-list product, with no form element on it beyond a menu toggle and a cookie Accept -- the mailing-list signup the copy invites is not even wired up. The dataset records no opt-out URL, no opt-out email and an opt_out_method of 'unknown' for this row (Freemium Data Services, LLC), which is consistent: there is nothing to opt out of yet and nowhere to do it. Recorded as no-surface rather than undecided because the state is unambiguous and self-described. It is worth a recheck if the dataset is ever refreshed, though -- a pre-launch broker is the one category that can turn into a live one without warning, which is the opposite of emerges-com elsewhere in this module, a broker that has shut down.

### `saymine-io`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-23: this row's domain is not a broker at all. saymine.io is Mine, a privacy-request SERVICE; the dataset URL cognism.privacy.saymine.io/cognism is Mine's hosted privacy centre for a DIFFERENT company, Cognism. Flagging as a probable dataset defect: the row should almost certainly be keyed on cognism.com, with saymine.io as the opt-out host. Either way Mine publishes no people-search surface, and the Cognism database is a paid B2B product queried behind a login.

### `smartmove-us`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_BLOCKED`

  > FCRA-REGULATED BACKGROUND SCREENING -- see the category note above NO_OPTOUT_SURFACE in this module for why this whole class gets no recipe. Verified 2026-09-23: the recorded do-not-sell URL never renders -- smartmove.us serves a Cloudflare interstitial ('Performing security verification ... This page is displayed while the website verifies you are not a bot', Ray ID a3fd3ab4892b31a9) and redirects with a __cf_chl_rt_tk challenge token. So the page is walled as well as being in the exempt class. SmartMove is TransUnion's landlord-facing tenant- screening product; a report is pulled by a landlord with the applicant's consent, and the consumer's recourse is the FCRA channel rather than this link. DATASET NOTE: the row is named 'CTAM Leadshare Corp.' with contact zell@ctam.com, which matches neither TransUnion nor SmartMove -- flagged, not fixed.

### `socialgist-com`

- **scope:** dataset | **kind:** rebrand-or-domain-change | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-23. socialgist.com redirects to socialgist.ai -- a domain change the dataset does not record, flagged here and left unfixed in data/source-brokers.json. The site that answers is a brochure for social-conversation data sold to AI and intelligence platforms, and it contains no form element at all: the only controls in the whole document are the cookie banner's Accept and Decline. There is no privacy request page, no rights page and no do-not-sell link anywhere on it. This matches what the dataset already says -- method email, info@socialgist.com -- and the finding is that the web side is genuinely empty rather than walled. Note that info@ is a general enquiries address rather than a privacy one, so a person writing to it should not assume it reaches a rights process.

### `spyfly-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. The dataset's opt_out_url (/help- center/privacy) is a readable privacy policy with no inputs; the real surface is the 'Do Not Sell My Personal Information' link it repeats five times, pointing at /help-center/privacy- requests. That is also exactly what SpyFly's own support reply (recorded in the dataset note, 2026-08-24) told the requester to use. Requesting that page returns HTTP 403 and a Cloudflare interstitial -- 'Performing security verification ... This website verifies you are not a bot', challenges.cloudflare.com loaded, Ray ID a3fd4f7d8c2531a9. The form never renders, so there is nothing to transcribe. Note the shape of it: the policy page serves fine and only the request page is challenged, which is the same asymmetry seen on several brokers this pass -- the reading is open and the acting is walled. privacyinfo@spyfly.com is on file as a human channel if the wall holds.

### `take5mg-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-23: the domain is effectively gone. take5mg.com, www.take5mg.com and the plain-http form all fail identically with an EXPIRED TLS CERTIFICATE, and a search result for the domain is titled 'take5mg.com Domain for sale', so the host is parked rather than serving Take 5 Media Group content (Take 5 was acquired by Advantage Solutions in 2018, which is why the dataset files it under Advantage Sales & Marketing LLC). A dead, parked domain has no search surface.

### `transunion-com`

- **scope:** dataset | **kind:** rebrand-or-domain-change | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Verified by browser render 2026-09-23: same finding as experian- com. www.transunion.com/consumer-privacy renders an FAQ accordion ('How do I make a data privacy request?', 'Where can I learn about my consumer rights?') and no request form -- the only two forms are copies of the header site search. reCAPTCHA v3 is loaded on the page (api.js?render=6LfUswssAAAAAEy6MG6LCW72Avmkx2Yohnv2oQfY plus a recaptcha-cloudservice element), so whatever the accordion links out to is captcha-backed. Not resolved: which TransUnion property accepts a marketing-data suppression as opposed to a credit-file request, and whether the acquired Neustar identity- graph data the dataset notes mention is covered by the same request or needs a separate one. Second channel for a human: privacy@transunion.com.

### `trufactor-io`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: https://trufactor.io/ timed out (Page.goto, 30000ms) with no response at all. TruFactor was an SK Telecom-backed mobile-data venture, so a dead host is a plausible end state for it, but 'timed out once' is not evidence of that and is not being written up as if it were. Retry; if it stays dark, the question for the defects list is whether the company still exists, which is a research question and not one this tool can answer from a fetch.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: connection to https://trufactor.io/ timed out after 30s with no response at all -- not a challenge page, not an error page, nothing. TruFactor was an SK Telecom-backed mobile-data venture; a dead host is a plausible end state, but a single timeout is not evidence of that and no such conclusion is recorded here. Retry before treating this row as anything.

### `winwithoptimal-com`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, because the company behind this row has been renamed and the checklist is keyed to the old domain. DATASET DEFECT, flagged here and deliberately NOT fixed in data/source-brokers.json: the row is 'Dspolitical, LLC' at winwithoptimal.com with info@dspolitical.com. Requesting winwithoptimal.com/privacy-policy/ redirects to https://www.onemagnify.com/privacy-policy -- DSPolitical / Optimal is now OneMagnify. The old domain still serves its own marketing pages, so this is a live rebrand mid-flight rather than a dead domain, which is why the redirect only shows up on the policy path. What the recorded opt_out_url actually is: /opt-out-of-advertising/ is an EXPLAINER about managing cookies. Its only first-party form is #footerForm, a single-email Gravity newsletter box (gf_field_3_1) -- a marketing signup, not an opt- out, and a careless reader could easily write a recipe against it. The opt-out links it does give are all third-party: Google's gaoptout, adsrvr.org, Yahoo's device dashboard. Cookie consent is OneTrust. Next step for whoever picks this up: research OneMagnify, not winwithoptimal. A first pass over onemagnify.com/privacy-policy found no anchor matching opt-out / do-not-sell / request / rights at all, so the rights channel there is probably a mailto or a portal named something else in the policy prose, and the policy text needs reading rather than its links scanning. If the surface is found, consider whether this row should be re-keyed to onemagnify-com.

### `worldpay-com`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and the reason is a tangle in the dataset row rather than anything the broker has done. DATASET DEFECT, flagged and NOT fixed: the row is named 'Efunds Corporation', keyed to worldpay.com, with chexsystems.compliance@fisglobal.com as the contact. Those are three different things. eFunds is the company behind CHEXSYSTEMS, the banking consumer reporting agency that decides whether someone can open a checking account. Worldpay is a payments processor. Both passed through FIS ownership, which is how they came to share a row, but the consumer-facing reporting product is not on worldpay.com at all -- and ChexSystems, like earlywarning-com in this module, is an FCRA agency whose file is not something a consumer can opt out of. What was actually found at the recorded domain: worldpay.com carries 'Do not sell or share my personal information' pointing at privacy.worldpay.com, which resolves to /policies and is a Transcend-powered privacy centre ('Powered by Transcend') offering 'Make a Privacy Request' and 'View Past Requests'. It is a single-page app -- /request 404s WITHIN it, so the request flow opens from the button rather than from a URL, and nothing could be transcribed without driving it. Next steps, in order, because they are two different jobs: (1) drive the Transcend portal from the button, enumerate the request-type options and the fields, and re-check for a captcha at that stage -- none is loaded on the landing page, which proves nothing; (2) decide whether this row should be re-keyed to chexsystems.com, and if so whether it belongs with earlywarning-com as an FCRA agency with no opt-out rather than here.

