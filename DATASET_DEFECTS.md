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

165 findings across 131 brokers.

| scope | findings |
| --- | --- |
| broker-surface | 11 |
| dataset | 86 |
| unreachable | 68 |

| kind (keyword guess) | findings |
| --- | --- |
| parked-or-defunct | 69 |
| unclassified | 54 |
| dead-url | 17 |
| broker-surface-defect | 10 |
| entity-mismatch | 8 |
| rebrand-or-domain-change | 5 |
| contact-address-oddity | 1 |
| stale-200 | 1 |

## Findings by broker

### `acuityads-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and there are three separate reasons to leave it open. FIRST, A REBRAND THE DATASET DOES NOT RECORD: privacy.acuityads.com resolves through to illumin.com. AcuityAds now trades as illumin, and every live URL is on the new domain. Flagged, not fixed. SECOND, the form is real but barely legible from the outside. illumin.com/opt-out/ hosts a HubSpot form inside an ABOUT:BLANK frame -- injected by script rather than served from a URL -- containing a single required input[name='email'] with a per-render id (email-56f7074b-6a42-488a-abca-2066a9726da2) and a Submit. No captcha script was seen, but the frame is about:blank, so that observation covers the parent page and NOT reliably the frame's own contents. Under this module's standing rule that is not a finding of 'no captcha'. A recipe would also have to address a frame with no URL and an id that changes per render -- neither impossible nor stable. THIRD, AND THE BEST FINDING HERE: the consent page carries UNREPLACED COOKIEBOT TEMPLATE PLACEHOLDERS. Its links include a literal '[#DSR_FORM_URL_TEXT#]' pointing at 'illumin.com/opt-out-success/[#DSR_FORM_URL#]', alongside '[#IABV2SETTINGS#]'. The DSR form URL -- the data subject request link, the thing a person on a privacy page is looking for -- was never configured, so the banner offers a link to a page that cannot exist. That is a real, checkable defect in the broker's published rights channel, not a rendering artifact. itops@acuityads.com is the dataset contact; note it is an operations address, not a privacy one.

### `agrgroupinc-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-23: the domain does not resolve. Both agrgroupinc.com and www.agrgroupinc.com fail DNS with getaddrinfo ENOTFOUND, so no page of this broker exists to carry a search surface. The company is real -- CA data-broker registration 186616, All Global Resources, LLC, Henderson NV, privacy@agrgroupinc.com -- but it is a registration with no live website, which is also why the dataset lists it as email-only. If the domain ever comes back this call should be revisited.

### `alchemer-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-24 by browser render, and this row carries a dataset defect as well as a block.

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > DATASET DEFECT: the recorded opt_out_url points at Alchemer survey 8249180; it redirects to survey 8534043 ('Do Not Sell Request - 2026'). The survey is reissued per year, so the recorded id will rot again. Whatever eventually consumes this row should follow the redirect rather than pin the id.

### `assurance-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23: assurance.com serves an EXPIRED TLS CERTIFICATE on both the homepage and the dataset's privacy- practices URL, so no page was rendered on either leg. Assurance IQ is a Prudential-owned insurance-quoting marketplace, which would ordinarily point at no-surface, but nothing was observed. Note this is the second row in this batch behind an expired certificate (see take5mg-com, which turned out to be a parked domain) -- so check first whether assurance.com is still a live business or a lapsed one.

### `backgroundchecks-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > FCRA CATEGORY, verified 2026-09-23, and folded into the shared background-screening treatment rather than given a recipe. backgroundchecks.com sells pre-employment screening reports to employers. The dataset's /privacy path returns a genuine HTTP 404 ('This is a 404 error, meaning this link doesn't exist'), and the only frame on the page is a HubSpot chat widget. Flagged as a dataset defect; not fixed. More to the point, the routes the site does offer are the FCRA ones, visible in its own navigation: 'Get a Copy of Your Background Report' and 'Dispute Background Report'. Those are the statutory file-disclosure and dispute rights against a consumer reporting agency, and they are not opt-outs. A CRA regulated under the FCRA cannot simply delete a person from its files on request the way a marketing list broker can -- which is why this module treats the whole category as no-surface with an explanation rather than as a broker refusing to cooperate. The distinction to preserve for anyone reading this row: unlike g2risksolutions-com, which is a FURNISHER feeding data into TransUnion's files, backgroundchecks.com compiles and issues reports itself. Both are FCRA entities; only the latter holds a file a person can demand a copy of. support@backgroundchecks.com is published and is the right channel for a disclosure or dispute request.

### `beeswax-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > That host is broken. Over HTTPS it fails with ERR_CERT_COMMON_NAME_INVALID; inspecting the certificate shows it is issued for segment.prod.bidr.io (SANs: segment.prod.bidr.io, apac.segment.prod.bidr.io) and does not cover optout.prod.bidr.io at all. Per the standing rule from listsonline/Everleads in batch 19, plain HTTP was tried before calling it dead -- and here HTTP does not rescue it: it returns 301 Moved Permanently straight back to https://optout.prod.bidr.io:443/optout, i.e. back into the certificate error. There is no reachable path.

### `bidr-io`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: the page could not be reached at all, and the failure is specific enough to be worth recording. A headless Chromium navigation to the dataset's opt_out_url (optout.prod.bidr.io/optout) failed with net::ERR_CERT_COMMON_NAME_INVALID -- the TLS certificate served on that host does not cover that name, so no browser will load it without an explicit override, which this tool will not do. That is a misconfiguration on Beeswax's side rather than an anti-bot wall, and it means the opt-out is effectively unavailable to any ordinary consumer using an ordinary browser -- which is itself the finding. Next pass: recheck whether the certificate has been fixed; if it has not, this row arguably belongs under NO_OPTOUT_SURFACE, because an opt-out nobody can open is not an opt-out.

### `blisspointmedia-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, for a network reason rather than a research one: www.blisspointmedia.com failed to RESOLVE (net::ERR_NAME_NOT_RESOLVED) from a headless Chromium on this host, so no page was reached. This is the second row in the dataset to fail this way (see nuwber-com), and the two should be rechecked together from a different network before either is called dead -- a DNS failure is not a 404 and is not an anti-bot block. If it does turn out to be gone, note that Bliss Point Media was acquired and may now trade under another name, the same trap corelogic-com fell into.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23: www.blisspointmedia.com did not RESOLVE (net::ERR_NAME_NOT_RESOLVED) from a headless Chromium on this host, so nothing about either leg can be stated. Recheck from a different network before calling it dead, and check whether the company now trades under another name. Same situation as nuwber-com; see the opt-out leg's entry.

### `box-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT 2026-09-24, and this row should probably not exist in the form it exists in. It is recorded as a DATASET DEFECT.

- **scope:** dataset | **kind:** unclassified | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-24 by rendering the recorded URL. No consumer-facing lookup exists there -- but read the opt-out leg before trusting this row, because the row itself is a dataset defect: box.com is Box, Inc., a file-sharing host, and the company named on the row is EAB Global, whose domain is eab.com. What was actually observed is a PDF viewer showing EAB's privacy policy. The leg is closed as to box.com, which offers no lookup and is not a broker; whether EAB offers one at eab.com is a separate question this row cannot answer.

### `brandwatch-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > DATASET DEFECT: the recorded opt_out_url, www.brandwatch.com/confirmation/legal-data/, is not a form. It is the POST-SUBMISSION THANK-YOU PAGE: it renders 'Thank you! We'll be in touch. We'll be in touch soon. In the meantime, why not check out some of our latest content.' A consumer following that link would see a confirmation for a request they never made, and an automated agent keying on confirmation text would read it as SUCCESS without having submitted anything. That is the most dangerous shape a bad URL can take in this dataset, and it is worth generalising: success markers must be checked against the page that a submission actually navigated TO, never against a URL taken from the dataset.

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

### `ca-gov`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_OUT_OF_SCOPE`

  > Verified 2026-09-24 by browser render. This row is a DATASET DEFECT, and a consequential one.

### `californiacourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > DATASET DEFECT: the row records opt_out_method 'web-form' with verification_step 'CAPTCHA required on the opt-out form'. There is no form and therefore no captcha on it. The recorded opt_out_url (/optout) is a rights-information page.

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

### `cashmereai-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > DATASET DEFECT: the recorded opt_out_url www.cashmereai.com/privacy returns a hard 404 ('This page wandered off the trail'). The live policy is at /policy/privacy-policy.

### `catalogchoice-org`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_OUT_OF_SCOPE`

  > Verified 2026-09-24 by browser render. This row is a DATASET DEFECT of the same species as ca-gov: Catalog Choice is not a data broker, it is a NONPROFIT OPT-OUT SERVICE. Its own pages say so -- 'We're a non-profit organization working to stop [junk mail]', 'Stop Junk Mail For Good', 'Free service to cancel unwanted paper catalogs' (the last from the dataset's own note, which describes the service correctly while still filing it as a broker).

### `catalyzeai-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, verified 2026-09-23: the row's opt_out_url https://www.catalyzeai.com/opt-out returns HTTP 404 ('Page not found. The page you are looking for doesn't exist or has been moved.'), and the dataset's own notes already record that the broker's email bounced on 2026-08-20. That leaves this row with NO working contact of any kind. Not fixed in data/source- brokers.json, only recorded here. What is left to try: find a current privacy policy on catalyzeai.com and read the rights section off it, or establish that the company has folded or been absorbed -- either answer resolves the row, and neither was established today.

### `cdkglobal-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified by browser render 2026-09-23: CDK's 'Do Not Sell or Share My Personal Information' footer link has an EMPTY href -- it is a JavaScript consent-widget trigger, not a page -- and the privacy statement scopes it explicitly to tracking technologies: 'You may manage your preferences on the tracking technologies deployed on the Site by clicking on the Do Not Sell or Share My Personal Information link at the footer'. Its own text says 'We do not sell your Personal Information' for other purposes, and every actual rights request (deletion, access) is directed to a contact address rather than a form. So there is no web surface that suppresses a person's records here, only a cookie preference for this website's visitors. The remaining channel is mailbox-only, which this codebase cannot represent. Dataset contact for a human: james.kinzer@cdk.com.

### `citydata-ai`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > DATASET DEFECT: the recorded opt_out_url citydata.ai/privacy/do_not_sell_personal_information/ returns a hard 404 ('Page Not Found'). The live policy is at /privacy/.

### `civisanalytics-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, verified 2026-09-23: the row's opt_out_url https://www.civisanalytics.com/privacy-policy/supplemental- privacy-notice/ returns HTTP 404 ('The page you are looking for doesn't exist'), serving only a cookie banner. This is a circular dead end, because the dataset notes record that dataprotection@civisanalytics.com replied that they cannot process requests until THIS form is completed -- the form they point at no longer exists. Not fixed in data/source- brokers.json, only recorded here. Next step for a researcher: locate the supplemental notice at its current path under civisanalytics.com/privacy-policy and quote the real request surface back to that mailbox.

### `clustrmaps-com`

- **scope:** dataset | **kind:** unclassified | **from:** `search_forms.SEARCH_UNDECIDED`

  > What the dataset asserts, for whoever rechecks: ClustrMaps publishes name- and address-keyed residence records, so a search surface is very likely. To resolve: retry from the deployment host. If it answers there, this row and the opt-out leg both resolve in one pass; if it refuses there too, the next question is whether the domain still resolves and serves anyone at all, which would be a dataset defect rather than a mapping verdict.

### `co-ke`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > DATASET DEFECT, and a different cause from the ones logged so far. The row's domain field is the literal string 'co.ke' -- Kenya's public suffix, not a domain. The company's actual domain is metropol.co.ke. Because this module keys everything by a slug derived from the domain, the permanent key for Metropol is 'co-ke', which is both wrong and collision-prone: ANY other Kenyan broker added later would slug to the same key and silently overwrite this row.

### `coloradocourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > DATASET DEFECT, inherited by every site in the network: the row records opt_out_method 'web-form' with verification_step 'CAPTCHA required on the opt-out form'. There is no form and therefore no captcha on it.

### `com-co`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, identical in kind to co-ke: the row's domain field is the literal string 'com.co', Colombia's public suffix, not a domain. The registrable domain is datacredito.com.co. The permanent key for this row is therefore 'com-co', which is wrong and would collide with any other Colombian broker added later. Two instances of public-suffix mis-parsing in one batch; see co-ke for the full note.

### `completemailinglists-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > DATASET DEFECT, verified 2026-09-23: the row's opt_out_url https://www.completemailinglists.com/node/3697 returns HTTP 404. What is behind it is worth recording: the 404 page is a half- finished template whose navigation still reads 'Menu Item One / Menu Item Two / Menu Item Three', so the site appears to have been rebuilt without its rights pages being carried across. Its sibling completemedicallists.com DOES publish a working CCPA form at /ccpa.php, so the obvious next step is to check whether completemailinglists.com serves the same /ccpa.php form -- if it does, this row resolves immediately. Not fixed in data/source- brokers.json. Dataset contact: ewoolf@completemailinglists.com.

### `connecticutcourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Same dataset defect as its siblings: 'web-form' with a CAPTCHA recorded on a form that does not exist.

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

### `date-detective-app`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-25 -- nothing could be read, for the reason written up under search_forms.SEARCH_UNDECIDED['date- detective-app']: the dataset's opt-out host mobile.date- detective.app fails TLS from a real browser (net::ERR_CERT_COMMON_NAME_INVALID) because it answers on 443 with an Azure App Service default wildcard certificate (CN=*.msha-slice-6-wus2-1-ase.p.azurewebsites.net, issued to Microsoft Corporation) rather than a certificate for its own name; the apex does the same with the dm1 slice. The recorded opt_out_url is /investigate-opt-out?origin=website, which by this module's standing caveat is exactly the shape that can ACT on navigation -- it was requested once, deliberately and alone, and it never got as far as a handshake, so nothing was fired and nothing was read. Combined with the dataset's note that privacy@date-detective.app hard-bounced on 2026-08-21, this row has no working channel at all right now. Recheck is a single TLS handshake; if a custom binding appears, probe the opt-out URL one at a time again.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > Reachability failure, 2026-09-25, and the cause is worth recording precisely because it is not an anti-bot wall and not a dead domain. Both https://date-detective.app/ and the host the dataset records for the opt-out, https://mobile.date- detective.app/, fail TLS from a real browser with net::ERR_CERT_COMMON_NAME_INVALID. Reading the certificates directly explains why: each host answers on 443 with an AZURE APP SERVICE DEFAULT WILDCARD -- CN=*.msha- slice-6-dm1-0-ase.p.azurewebsites.net for the apex and CN=*.msha-slice-6-wus2-1-ase.p.azurewebsites.net for mobile, both issued to Microsoft Corporation -- so the app is up but no custom hostname binding or managed certificate was ever attached to it. Same shape as the optout.prod.bidr.io row: a certificate whose name does not match the host, distinguished from a DNS failure. Nothing can be read through it, hence no verdict. The dataset also records that privacy@date-detective.app hard- bounced on 2026-08-21, so this row currently has no working channel at all; a recheck is cheap (one TLS handshake) and should be redone rather than re-researched.

### `dealersocket-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.OPTOUT_OUT_OF_SCOPE`

  > Verified 2026-09-25, and it is the GUMGUM WRONG-REQUEST-TYPE PROBLEM again, this time without a captcha to hide behind. DealerSocket's footer 'Do not sell my personal data' goes to Solera's group DSAR portal (globaldsar.solera.com/webform/20bbc57b-.../1eafe8e0-..., headed 'DealerSocket, LLC (US)'), which rendered fully: a required 'Request:' autocomplete, required Full Name and Email, and optional country code, phone, country, address, city, state, zip and a details textarea. No captcha script and no captcha element were seen on this render. TWO REASONS IT IS OUT OF SCOPE ANYWAY. First, the form opens with a required identity question, 'I am or was:', whose options are Job Applicant, Employee, Contacted by the company, Employed by a Vendor, Employed by your Customer, Other. A person whose data DealerSocket holds because a car dealership fed it into their CRM is none of those; 'Contacted by the company' is a guess and 'Other' is an admission that the form was not built for them. Picking one on a user's behalf asserts a relationship they did not state. Second, mechanically: not one control on that form has a name attribute -- they are Angular components addressed only by id (requestTypesDSARElement, firstNameDSARElement, emailDSARElement, ...) and the request type and state are comboboxes with 'Clear the ... field' buttons rather than selects, so 'Request:' has to be chosen from a popup list this pass never opened. The page does offer a documented human channel: a toll-free number, (855) 839-8020, stated for California do-not-sell requests. The dataset's opt_out_url for this row (/privacy-policy/) is the policy that links here.

### `deepsync-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-25, and this row is a DATASET DEFECT worth reading before trusting any 'no CAPTCHA' note in source- brokers.json. The row records verification_step as 'identity verified after submission via dynamically-generated knowledge questions (no CAPTCHA on the form itself)'. Rendering privacy.deepsync.com shows a CLOUDFLARE TURNSTILE on the form itself: challenges.cloudflare.com/turnstile/v0/api.js is loaded and a .cf-turnstile element is in the DOM. That is precisely the 2026-09-23 accurateappend/atdata failure repeating in the source data, and it is why this leg is blocked. The form is otherwise a good one -- #pii_form POSTing to /request/opt-out/submit, request_type radios ('opt_out', 'deceased', 'representative'), first_name, last_name, repeatable emails[] and phone fields, a primary address with a 50-state select, and a step-by-step PDF guide -- so if the Turnstile is ever cleared this is a strong staging candidate. Navigation to the portal root rendered the form and did not submit anything. Deep Sync also layers post- submission knowledge-based identity questions on top, which would be a second, independent reason this cannot run unattended.

### `degree-me`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23: the domain serves nothing. degree.me has NS delegation to AWS Route53 (ns-1086.awsdns-07.org, ns-1685.awsdns-18.co.uk, ns-236.awsdns-29.com, ns-918.awsdns-50.net) but NO A or AAAA record resolves, confirmed by dig both ways, and curl returns http_code 000. This looks like a dormant registration tied to ACE Agents Inc. / academixdirect.com. It sits here rather than under NO_SEARCH_SURFACE because a domain that does not resolve today may resolve tomorrow; recheck resolution before deciding.

### `dehashed-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-25, because the site is mid-rewrite and the recorded surface is simply gone. https://www.dehashed.com/optout returns a genuine HTTP 404 rendering DeHashed's own '404 / Page not found' template -- not a wall, not a redirect. DATASET DEFECT: this row records opt_out_method 'web-form' at that URL; there is no page there, so there is no form and no captcha on it. The 404's own 'Quick Navigation' links were followed and /faq 404s as well, and the homepage banner explains the state of things: 'Welcome to 4.0 -- Please be patient as records are gradually indexed over the coming weeks'. A site whose own navigation 404s is not evidence that a removal route does not exist, which is why this is undecided rather than no-surface; the plausible routes (a support ticket, or removal from inside an account) both sit behind the registration wall recorded on the search leg. The dataset's support@dehashed.com is the only channel that can be tried today. Recheck once 4.0 settles.

### `delawarecourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering /optout/ and /do-not-sell- share-my-personal-information/ and reading both in full. There is no opt-out form, and this row is the same page-for-page as californiacourtrecords-us, coloradocourtrecords-us and connecticutcourtrecords-us -- see the California entry for the full write-up. /optout/ ('Your Privacy Choices') is a rights explainer that points at the do-not-sell page; the do-not-sell page splits into two routes and neither is a form: cookie-level opt-out goes to the TrustArc cookie preferences dialog, and RECORD removal is email-only -- 'please submit your request by emailing privacy@courtrecords.us... Please include your first name, last name, state, and city in your request so that we can identify the appropriate record'. Mailbox-only, hence this bucket. DATASET DEFECT: the row records opt_out_method 'web- form' with verification_step 'CAPTCHA required on the opt-out form'. There is no form. reCAPTCHA api.js does load on both pages, which is exactly how a static skim would manufacture that claim. The same booby trap is present here too -- 'Submitting this form will result in the removal of only the specific records you select', left over from a form that is not on the page -- and the request is per-record: 'If you have additional records appearing on our website, each record must be submitted separately.'

### `digdevdirect-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-24: digdevdirect.com is a PARKED DOMAIN. The page is GoDaddy's free parking shell -- 'digdevdirect.com is parked free, courtesy of GoDaddy.com', a 'Get This Domain' button, and keyword-ad filler for Real Estate, Apartment For Rent, Personals and Cheap Airfare. There is no site behind the name.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Third parked domain in the module, after idengine-com and logiq-com, and worth one distinction: this is FREE PARKING rather than an auction listing. The registration is still held, so the owner has not necessarily given the name up -- a business that let its site lapse while keeping the domain looks exactly like this. That makes a future recheck slightly more likely to find something than it would be for an auction listing, and it does not change the present answer, which is that Media Direct publishes nothing here at all.

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

### `endgame-io`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering endgame.io/privacy in full. There is no opt-out form -- the page renders zero forms and zero inputs -- and the policy instead prescribes a mailbox with per- request SUBJECT LINES, which is unusually specific and worth recording verbatim for anyone writing to them: deletion is 'please email privacy@endgame.io with the subject Forget me', correction is the same address 'with the subject Personal Information Correction', and 'if you wish to opt out of the sale or sharing of your personal information, please email privacy@endgame.io with the subject Personal Information Sharing Opt-Out'. legal@endgame.io is also published. Mailbox-only, hence this bucket. DATASET DEFECT: the row records opt_out_method 'web-form' at /privacy and no opt_out_email; it should carry privacy@endgame.io. Scope note that explains the shape of this row: Endgame processes business contacts' data as a service provider on its customers' instructions and says the customer's own notice governs, so a request here may be forwarded rather than actioned; the only browser-level route the policy offers is the DAA's aboutads.info/choices, which is this dataset's optout-aboutads-info row and not an Endgame channel.

### `experian-com`

- **scope:** dataset | **kind:** rebrand-or-domain-change | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Verified by browser render 2026-09-23: the dataset opt_out_url www.experian.com/privacy/opting_out is a rights EXPLAINER, not a request surface -- the only form on the rendered page is the site-wide business search (input[name=q]). An invisible reCAPTCHA is live on it anyway (recaptcha__en.js plus a .grecaptcha-badge and a g-recaptcha-response textarea outside any form), which is the second time this sweep that a static fetch would have reported a captcha-free page. Experian also runs several DIFFERENT consumer channels that are easy to confuse and were not resolved here: the FCRA prescreen opt-out (optoutprescreen.com, itself blocked -- see its own entry), a marketing-mail opt-out, and a CCPA/state-rights portal. A future researcher needs to establish WHICH surface actually suppresses Experian Marketing Services data (including the acquired Tapad identity graph, which the dataset notes were merged into this row) and whether it can be reached without an identity-verified login, since the credit-file side certainly cannot. Second channel for a human: privacy@experian.com.

### `exploreatlas-io`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23. www.exploreatlas.io/privacy returns HTTP 200 whose body reads 'This page couldn't be found. You may not have access, or it might have been deleted or moved.' That phrasing is a Notion or Super-style hosted-site message rather than a web-server 404, so the page was published at some point and has since been unpublished, deleted or made private. Recorded as undecided rather than no-surface for two reasons. The site itself was not examined beyond the recorded privacy path, so a rights page may exist elsewhere on the domain. And the row has a bigger question hanging over it than a missing page. DATASET DEFECT, flagged not fixed: the row's domain is exploreatlas.io but its contact is SCOTT@HUNTCLUB.COM -- a personal address at an unrelated company. Hunt Club sells recruiting services; Atlas is a separate product name. Either the row conflates two companies, or Atlas is a Hunt Club property and the dataset records the parent's contact without saying so. As with the forms.gle row in the previous batch, acting on it risks sending a person's opt-out to a company that holds nothing about them. Establishing which company this row is about is the first step, before any further page-hunting.

### `familysearch-org`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-25 by browser render, and blocked for the same reason as the search leg. The recorded opt-out URL https://www.familysearch.org/en/legal/privacy answers 200 with an entirely empty main document (title '', zero characters, zero forms); what actually renders is an Imperva/Incapsula child frame titled 'Captcha Required' loading js.hcaptcha.com/1/api.js with an h-captcha widget and both g-recaptcha-response and h-captcha-response textareas. The privacy notice itself was never readable, so whether FamilySearch offers a form, an account-gated removal or a mailbox could not be observed at all -- but the wall is on every visit, which is what this bucket is for. DATASET NOTE: the row records opt_out_method 'web-form' on a page no browser here has ever seen; treat that claim as unverified.

### `faraday-ai`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-25, and it is the SAME failure that gave this workstream its per-target timeout: https://faraday.ai/privacy-options WEDGES THE BROWSER. Probed deliberately in a small run for exactly this reason, and the target hit TargetTimeout after the 75-second budget with nothing recorded -- no status, no title, no text -- reproducing on the .ai domain what www.faraday.io/privacy-options did twice on 2026-09-23 (the incident that made frame.evaluate hang forever and lost eight already-probed targets). The fixes held: the run was capped at 75s and the other targets in it were written out intact, so the cost this time was one row, not sixteen. DATASET NOTE: the row's own note already warns that opt_out_url may be a general privacy page rather than a consumer opt-out form; that remains unverified either way. Anyone retrying should expect the wedge and read the page some other way.

### `flashintel-ai`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. The dataset's flashintel.ai/dont-sell-my- information redirects to www.FLASHLABS.ai/dont-sell-my- information -- a rebrand the dataset does not record, flagged here and left unfixed. The dataset's contact for the row, legal@myflashcloud.com, is a third distinct name again. The form is real: Full Name, Current Company, Profile URL and Business Email, all four required, under a 'Submit Request' button. Blocked by reCAPTCHA -- a g-recaptcha-response textarea inside the form with both api2/anchor and api2/bframe frames attached. Worth recording even past the captcha, because it is a second, independent obstacle of a kind this sweep keeps meeting: the form asks for PROFILE URL and CURRENT COMPANY, both required. This is a B2B contact enrichment product, so the record it holds is keyed to a professional profile rather than to a household. resolve_fields has no source for either, and a profile URL is not something the tool could infer -- the user would have to supply it. Same shape as the MAID-keyed brokers (complementics, collectivedata, datafy, factori): the identifier the broker files you under is not one this codebase collects.

### `floridacourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering /optout and reading it in full. There is no opt-out form, and this row is page-for-page the same as californiacourtrecords-us, coloradocourtrecords-us, connecticutcourtrecords-us and delawarecourtrecords-us -- see the California entry for the full write-up. /optout renders 'Your Privacy Choices', a rights explainer with zero form controls that points at courtrecords.us/do-not-sell-share-my- personal-information/; record removal on the network is email- only to privacy@courtrecords.us with first name, last name, state and city, and is per-record. Probed as a potential bare- GET action link because the URL contains 'optout': it is NOT one -- navigation rendered an ordinary page and nothing fired, consistent with the same check on the sibling sites. DATASET DEFECT: this row records opt_out_method 'web-form' with 'CAPTCHA required on the opt-out form', on a form that does not exist here.

### `force-com`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. Two layers of confusion resolved, then a wall. DATASET DEFECT, flagged and NOT fixed: the row is e.Republic (a government-and-education media company) but is KEYED to force.com, which is Salesforce's hosting domain rather than anything e.Republic owns. The broker_id 'force-com' is therefore meaningless, and any future row hosted on Salesforce would collide with it. The recorded URL (erepublic.secure.force.com/PrivacyRequest/) is dead: Salesforce answers 'URL No Longer Exists'. So does erepublic.com/privacy- policy/. The live surface was found on the footer of e.Republic's own 404 page -- erepublic.my.salesforce- sites.com/PrivacyRequest/ -- i.e. the same app migrated from the retired *.secure.force.com hostname to the current *.my.salesforce-sites.com one. That form is genuine (a request- type select, name, phone, email, full address, a state select, a comments box and a declaration 'under penalty of...' checkbox) and is blocked three times over: * reCAPTCHA, via a hidden recaptchaToken input. * A HONEYPOT named almost plausibly: a hidden text input ending ':HomeAddressHP' -- the HP suffix being the only giveaway on a form that also asks for a real home address. * A TIMING TRAP, which is new in this sweep and worth naming: the form carries formLoadTime and TimeSpent inputs, so the server judges HOW LONG the form took to fill. A recipe that fills instantly is detectable even with every field correct and every honeypot avoided. And even past all three, the field names are Visualforce's positional auto-ids -- j_id0:j_id2:j_id3:j_id31:j_id36 and so on -- which renumber whenever the page is edited. This is the most fragile naming scheme the sweep has met, worse than Gravity's input_N. privacy@erepublic.com is the published channel.

### `forian-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-24. forian.com/privacy-consumer-requests/ carries a Divi (et_pb) contact form with a genuinely good structure: a 'Type of Request' select that includes 'Request to Opt-Out of Sale', a 'Who is Making this Request' select that -- unusually, and in welcome contrast to the gumgum-com wrong-request-type problem -- actually offers 'Consumer' and 'Website Visitor' alongside Healthcare Provider, Customer and Employee / Job Applicant. Then Name, Email Address, Address, an optional 'NPI #' and an 'Additional Information' textarea, under a Submit button.

### `forms-gle`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and this row has TWO dataset defects stacked on each other. Both are flagged here and neither is fixed. FIRST: the row's domain is 'forms.gle'. That is Google's URL shortener, not a broker. Slugged, it becomes the key forms-gle, which identifies no company and will collide with any other row whose opt-out happens to be a Google Form. The row's real subject is whatever company the form belongs to. SECOND, and worse: the form does not belong to the company the row names. The dataset's contact is privacy@REALEFLOW.com. The form at forms.gle/S7vW6zXPwgtnZ9ZF9 is titled 'GROWBOTS OPT-OUT REQUEST FORM' and its text is Growbots' throughout -- 'we will remove the profile and business information linked to this email from our database'. Realeflow sells real-estate investor leads; Growbots sells B2B sales prospecting. They are unrelated. Either the URL was copied into the wrong row or the email was, and there is no way to tell which from here. Acting on it would send a person's opt-out to a company that may hold nothing about them, while leaving the company that does hold something untouched. That alone makes it unsafe to automate. For the record, the Growbots form itself is a Google Form (mG61Hd, entry.1596228221) with a single required text input and the usual hidden fvv / fbzx / pageHistory / submissionTimestamp apparatus. Its labels are carried by aria-labelledby rather than by label elements, the same gap already recorded for clay-com and factori-ai -- the probe cannot read the question text, so which field is which is inferred, not observed. The form also states an out-of-band hop: 'upon filing one and CONFIRMING YOUR EMAIL, we will remove the profile'. Resolving this row starts with establishing which company it is actually about.

### `foundryco-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_OUT_OF_SCOPE`

  > OUT OF SCOPE, verified 2026-09-25 by browser render: the only do-not-sell actuator Foundry publishes is a JAVASCRIPT-ONLY CONSENT MODAL with no URL. The footer link on every page, labelled 'California: Do Not Sell My Information', has href 'javascript:window._sp_.usnat.loadPrivacyManagerModal(1316752)' -- a Sourcepoint US-National privacy manager invoked by function call against a numeric property id. There is no page to navigate to, no form element to transcribe and nothing to POST; a sibling link 'Privacy Settings' calls window._sp_.gdpr.loadPrivacyManagerModal(868952, 'vendors') the same way. That is the modal-wizard case this dict exists for, and it is also browser-scoped consent rather than record deletion. DATASET DEFECT, flagged and not fixed: the recorded opt_out_url foundryco.com/idg-privacy-policy/ REDIRECTS to /privacy-policy/ (the IDG-era path is gone, as is the IDG name -- the company trades as FoundryCo, Inc. now). For the record- level rights, both the privacy policy and the separate CCPA notice at foundryco.com/ccpa/ -- which confirms 'Foundry is registered as a data broker under the CCPA' -- decline to give a form at all: each right ends with 'please contact us using the contact details provided below' / 'in Section 1', i.e. a prose contact route via foundryco.com/contact-us/. Note also foundryco.com/california-privacy-rights/ is a genuine 404, so do not record that path. No captcha was seen anywhere, which is immaterial while there is no form.

### `freepeopledirectory-com`

- **scope:** dataset | **kind:** unclassified | **from:** `search_forms.SEARCH_UNDECIDED`

  > A REAL, UNWALLED, URL-ADDRESSABLE-LOOKING SEARCH FORM, transcribed 2026-09-25 by browser render, which directly contradicts this row's dataset note. https://www.freepeopledirectory.com/ carries form#form- submit.name-form, METHOD=GET with action on the site root, holding input#fname 'First Name', input#lname 'Last Name' and input#address 'City & State' (name attribute address_data) plus button#submit-button 'SEARCH'. No captcha script, no captcha element. DATASET DEFECT: the row claims 'Results load behind a Spokeo-style city/state wizard, so not URL-addressable' -- there is no wizard on the entry page, just a three-field GET. Left undecided because the results page was not driven, so the results host, the hit and no-hit markers and any count pattern are all still unknown, and this is a Spokeo-network property whose results may well be gated the way Spokeo's are. The opt- out leg is already settled (NO_OPTOUT_SURFACE).

### `fullcontact-com`

- **scope:** dataset | **kind:** rebrand-or-domain-change | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. platform.fullcontact.com/your-privacy- choices is a multi-step rights wizard -- five buttons outside any form (Access My Data, Correct My Data, Do Not Sell or Share, Limit Sharing Of My Data, Delete My Data) and no fields until one is chosen. Structurally identical to fideo-ai in the previous batch. Blocked by reCAPTCHA: gstatic's recaptcha script and google.com/recaptcha are both loaded on the landing step, before any field exists, so the challenge is attached to the flow rather than to a particular page. DATASET NOTE, flagged and not fixed: the row's contact is privacy@ziffdavis.com, not a fullcontact.com address. FullContact was acquired and its rights requests now route to Ziff Davis. That is correct rather than wrong -- but it means anyone reconciling this row by domain will think the address is a mistake, and it is worth knowing it is not. The page also names an authorised-agent route by email to privacy@fullcontact.com, which still resolves.

### `fusedleads-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: fusedleads.com could not be loaded at all. The navigation failed with net::ERR_CERT_DATE_INVALID -- the site's TLS certificate is expired or not yet valid. That is worth distinguishing carefully from the other failure modes in this module. It is NOT a DNS failure (the name resolved), NOT a refused connection (the handshake got far enough to present a certificate), and NOT an anti-bot wall (a wall serves a challenge page, which reads fine). The host is up and answering; its certificate is simply out of date. Every ordinary visitor is seeing the same browser interstitial, so this is a broker whose site is effectively unreachable to the public rather than one defending itself against automation. It is undecided rather than closed because certificates get renewed, often within days, and the site behind it is unexamined. A retry in a week is the whole next step. If it is still expired then, that is worth saying out loud in any escalation: a data broker whose opt-out channel is unreachable because it has not renewed a certificate is not offering one. The dataset records this row as email- method with greg@fusedleads.com, which at least does not depend on the website.

### `georgiacourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering /optout. Identical in every respect to floridacourtrecords-us and the rest of the CourtRecords.us network: same 'Your Privacy Choices' rights explainer with no form, same pointer to courtrecords.us/do-not- sell-share-my-personal-information/, same email-only per-record removal to privacy@courtrecords.us, and the same dataset defect ('web-form' plus a CAPTCHA recorded on a form that is not there). Navigating the /optout URL fired nothing. See californiacourtrecords-us for the full write-up.

### `getivydata-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: getivydata.com does not resolve (net::ERR_NAME_NOT_RESOLVED). UNREACHABLE for the defects list. Only the bare domain was in the dataset, with no opt-out path.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: getivydata.com does not resolve (ERR_NAME_NOT_RESOLVED), so nothing can be said about any surface. UNREACHABLE for the defects list.

### `governmentregistry-org`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Verified 2026-09-25 by rendering https://www.governmentregistry.org/opt-out on its own (the URL contains 'opt-out'; it is not a bare-GET action link, nothing fired). The page is titled 'Opt out of GovernmentRegistry' and headed 'Your Privacy Choices' / 'Remove my Personal Information', and it carries a form -- but the form's submit button reads SEARCH, not Submit, and its fields are input#firstName (required), input#lastName (required), input#city named town, and select#state limited to NINETEEN states (California, Colorado, Connecticut, Delaware, Indiana and the rest of the comprehensive-privacy-law states), against the 52-option state list on the site's own people-search form. So removal here appears to begin by finding your record and then picking it, which is the out-of-scope shape -- but that was not driven, so it is recorded as what was actually seen. No captcha script and no captcha element on the page. DATASET DEFECT: the row claims fields 'name, city, state; email optional for confirmation' -- there is no email field on this form at all. Privacy contact privacy@cisnationwide.com (Accucom / CIS Nationwide) remains the stated alternative.

### `granitelists-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: granitelists.com returns HTTP 403 with the page reading 'Account Suspended. This Account has been suspended. Contact your hosting provider for more information.' That is the hosting provider's own interstitial, not the broker's site. Distinguish this carefully from the other unreachable rows. It is not a bot wall (403 here is the host refusing to serve anyone), not DNS, and not a certificate problem as with fusedleads-com. The account behind the domain has been suspended, most often for non-payment or a terms violation. What makes it consequential rather than merely inconvenient: the dataset records NO opt-out URL, NO opt-out email and an opt_out_method of 'unknown' for this row. So there is no fallback channel to fall back to. If the company still holds data, there is at present no way whatsoever for a person to reach it. Undecided rather than closed because suspensions are reversible and the site behind it has never been seen. Recheck in a few weeks; if it is still suspended and still has no published address, that combination is worth escalating rather than filing.

### `greatlakeslists-com`

- **scope:** dataset | **kind:** stale-200 | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23, and the first finding is a DATASET DEFECT, flagged and deliberately not fixed. The recorded opt_out_url, greatlakeslists.com/opt_out_request.php, still returns HTTP 200 but no longer contains a form -- it renders the site's generic chrome and nothing else. A tool following the dataset would find an apparently healthy page with nothing on it and could easily record 'no surface'. The live surfaces are reached only from the footer: /do-not-sell-ca ('California Consumer Privacy Act Requests') and /do-not-sell-non-ca ('Opt Out Requests Web Form'). A 200 that quietly stopped being the page it used to be is a worse failure than a 404, which would at least announce itself. Both live pages are blocked by reCAPTCHA v3 -- api.js?render= with site key 6LeG5lAbAAAAAB2mnbwCEYHiihwLefn_Udbwksfe, plus the gstatic runtime and an anchor frame, on each. v3 is score-based and entirely invisible: there is no checkbox to click and no puzzle, which means an automated submission is not refused so much as silently scored down. That failure mode is particularly bad for this tool, because the request can appear to go through. Both are also ROLE-GATED wizards before any fields appear: 'Who is submitting this request? ... I am the person opting out / I am an authorized agent'. No field set was reached, so nothing beyond the choice step is recorded. The pages do offer a 'Check the status of your opt-out request' route, which is unusual and useful, and the dataset records no opt-out email for this row, so the web form is the only channel.

### `grin-co`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. grin.co/data-privacy-form/ returns HTTP 200 but the body is a Cloudflare interstitial -- title 'One moment, please...', text 'Please wait while your request is being verified...'. No form, no fields, nothing else in the DOM. This is the shape that most deserves care in this module, because it lies twice. The status code says success. The page renders without error. A probe that only checked for HTTP 200 and then counted form elements would report 'page loads fine, no opt-out form present' and the row would be closed as no-surface -- a conclusion that is exactly backwards, since the URL is named data-privacy-form and the form is almost certainly sitting behind the challenge. Recorded as blocked rather than undecided because the obstacle is deliberate and persistent: Cloudflare's managed challenge is aimed at precisely the kind of headless automation this tool performs, and waiting longer does not resolve it. The only honest statement about what is behind it is that nothing has been seen. The dataset records no opt-out email for this row, so there is no fallback channel to offer a user. A human with an ordinary browser will pass the challenge without noticing it, so the page is reachable to people and not to this tool -- worth saying plainly if the row is ever surfaced in a report.

### `growbots-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-25, because the recorded page renders EMPTY. https://www.growbots.com/do-not-sell-my-info/ answers 200 with the correct title ('Do Not Sell My Info - Growbots') and then serves no content: the entire document is 763 characters, all of it the site nav, the footer link list and a cookie banner, with zero forms, zero inputs, no email address and not one sentence about how to make a request. No captcha script and no captcha element either -- the page is not walled, it is hollow. DATASET DEFECT: the row records opt_out_method 'web- form' pointing at this URL; there is no form on it. Undecided rather than no-surface because a WordPress page whose body failed to render is a defect to recheck, not a confirmed dead end.

### `gumgum-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-23. GumGum's privacy policy has no form of its own; its 'DO NOT SELL', 'Do not Sell' and 'Exercise Your Rights' links all point to the same OneTrust DSAR webform on privacyportal-cdn.onetrust.com. That form was rendered and read. Blocked by reCAPTCHA -- api.js loaded, a g-recaptcha-response textarea inside the form, and both an anchor and a BFRAME attached, the latter meaning the checkbox-with-puzzle variant. THE WRONG-REQUEST-TYPE PROBLEM, and a clear-cut instance of it. The form is headed 'SUBJECT ACCESS FORM' and its first required question is 'I am a (an)' with the options: Prospective Employee, Client, Employee, Visitor, Other. THERE IS NO OPTION FOR A PERSON WHOSE DATA THE COMPANY COLLECTED THROUGH ADVERTISING -- which is everyone this dataset is concerned with. A consumer opting out of GumGum's ad targeting has never been its employee, client or prospective employee, and calling themselves a 'Visitor' asserts a relationship to gumgum.com that they very likely do not have. This is a generic HR-oriented OneTrust template pressed into service as an advertising-privacy channel, and picking any of its options would mean a recipe choosing a characterisation on the user's behalf. Recorded as blocked on the captcha, which is decisive on its own, but the request-type problem would independently keep it out of STAGED_RECIPES. Remaining fields are conventional: First Name, Last Name, Email, Country and a required Request Details textarea, with a request-type multi-select offering Opt out / Update Data / Info Request / Data Deletion / Object to Processing. Note the policy also links the NAI consumer opt-out, which is not GumGum's surface. talbert@gumgum.com is the dataset contact -- a personal address.

### `hartehanks-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-25 by browser render, and the recorded URL is dead. https://www.hartehanks.com/privacy/preferences 404s and lands on /newsroom/, and /privacy-policy/ 404s outright; the live documents are /privacy-highlights-march-2026/ and /privacy- statement-march-2026/, and both send the reader to a 'Preferences page' / 'Don't Sell My Info' link at https://privacy-in-action.hartehanks.com/. That host redirects to a OneTrust DSAR webform (privacyportal.onetrust.com/webform/a 1cc801d-a1ae-42ce-9f6b-2d394bf3e318/a4bc240d-e37d-427e-82b7- 1969417aacc0/, 'Welcome to the Harte Hanks Privacy Webform!') which is a real and thorough form -- subject type, salutation, required First Name and Last Name, middle name, company, required Country, Address, City, Postal Code, Email and Phone Number, optional State and additional email -- and it is CAPTCHA-WALLED: google.com/recaptcha/api.js loads explicitly, a g-recaptcha-response textarea sits in the form, and a child frame renders the visible 'I'm not a robot' reCAPTCHA v2 checkbox. DATASET DEFECT: the row claims 'Preferences webform verified live 2026-09-22' against a URL that 404s; the form is real but it is at a different host and it is behind a checkbox captcha. privacy@hartehanks.com and +1-800-541-5594 are offered as alternatives.

### `hawaiicourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering /optout. Identical to floridacourtrecords-us and georgiacourtrecords-us -- 'Your Privacy Choices' rights explainer, no form, pointer to the network do-not-sell page, email-only per-record removal to privacy@courtrecords.us, same dataset defect recording a 'web- form' with a CAPTCHA that does not exist here. Navigating the /optout URL fired nothing. See californiacourtrecords-us for the full write-up.

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

### `homedata-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-25. DATASET CLAIM FALSIFIED: the row says 'homedata.com itself now serves generic deepsync.com content', and it serves nothing at all. homedata.com and www.homedata.com both RESOLVE (15.197.142.173, an AWS Global Accelerator address) but the TCP connection never completes: Playwright timed out at 30s on domcontentloaded on both hostnames, and curl -L timed out at 25s with HTTP 000. Only privacy.homedata.com (54.185.236.12) answers, and it redirects to DeepSync's privacy portal. So there is no page under this domain to carry a lookup surface. Independently, DeepSync is a B2B identity-resolution and audience vendor that sells to marketers and offers no consumer- facing people search, so a working apex would not change this leg.

### `idahocourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering https://idahocourtrecords.us/optout/ on its own, one target at a time under the bare-GET action-link caveat. Navigating it fired nothing: it is the network's 'Your Privacy Choices' rights EXPLAINER, HTTP 200, about 6.8k of text, and it carries NO form and NO input of any kind. Its only routes off the page are the network do-not-sell page (courtrecords.us/do-not-sell-share-my- personal-information/), this row's own /do-not-sell-share-my- personal-information/ mirror, and email-only per-record removal to privacy@courtrecords.us. Identical to floridacourtrecords-us, georgiacourtrecords-us and hawaiicourtrecords-us; see californiacourtrecords-us for the full write-up. DATASET DEFECT, flagged and not fixed: the row records opt_out_method 'web-form' at idahocourtrecords.us/optout plus 'CAPTCHA required on the opt-out form', and there is no form on that page at all. One nuance worth recording against the earlier siblings' write-ups: this page DOES load Google's recaptcha api.js site-wide (cap[] reports it), but there is no captcha widget and nothing to submit -- the script is inert here, so the recorded CAPTCHA claim is still wrong, just not because reCAPTCHA is absent from the page's asset list.

### `idengine-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-23. idengine.com is a PARKED DOMAIN LISTED FOR SALE. The recorded opt-out path /dnsmpi/ redirects into GoDaddy's aftermarket and returns an Akamai 'Access Denied' for 'http://forsale.godaddy.com/forsale/idengine.com'. There is no site behind the name. This is the most complete form of absence in the module, and distinct from its neighbours: reachdata-com had not launched yet, emerges-com had shut down, granitelists- com is suspended and may return. A domain in a for-sale listing has been given up by its owner, and may shortly belong to someone entirely unrelated. That last point is the reason this is worth more than one line. The dataset records NO opt-out email for this row, so the URL was the only channel -- and if the domain is bought, /dnsmpi/ could later resolve to a live page belonging to a different company. A tool that retried this row mechanically could then submit a person's name and address to a stranger. Any future recheck of parked-domain rows should confirm OWNERSHIP, not merely that a page has appeared.

### `illinoiscourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering https://illinoiscourtrecords.us/optout/ on its own, one target at a time under the bare-GET action-link caveat. Navigating it fired nothing: it is the network's 'Your Privacy Choices' rights EXPLAINER, HTTP 200, about 6.8k of text, and it carries NO form and NO input of any kind. Its only routes off the page are the network do-not-sell page (courtrecords.us/do-not-sell-share-my- personal-information/), this row's own /do-not-sell-share-my- personal-information/ mirror, and email-only per-record removal to privacy@courtrecords.us. Identical to floridacourtrecords-us, georgiacourtrecords-us and hawaiicourtrecords-us; see californiacourtrecords-us for the full write-up. DATASET DEFECT, flagged and not fixed: the row records opt_out_method 'web-form' at illinoiscourtrecords.us/optout plus 'CAPTCHA required on the opt-out form', and there is no form on that page at all. One nuance worth recording against the earlier siblings' write-ups: this page DOES load Google's recaptcha api.js site-wide (cap[] reports it), but there is no captcha widget and nothing to submit -- the script is inert here, so the recorded CAPTCHA claim is still wrong, just not because reCAPTCHA is absent from the page's asset list.

### `imprintanalytics-io`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: https://imprintanalytics.io/limit-the-use-of-my-sensitive-personal-information/ cannot be loaded at all -- net::ERR_SSL_VERSION_OR_CIPHER_MISMATCH, i.e. the TLS handshake fails before any HTTP request is made. That is a server configuration fault on their side, not a block aimed at us and not a missing page: the URL's own slug is a CCPA right, so the surface was evidently meant to exist. Nothing can be said about its contents. Retry later; if it persists, this belongs in the consolidated defects list as UNREACHABLE rather than as a broker finding.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: TLS handshake fails outright (ERR_SSL_VERSION_OR_CIPHER_MISMATCH), so no HTTP request is ever made. Server misconfiguration on their side.

### `indianacourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering https://indianacourtrecords.us/optout/ on its own, one target at a time under the bare-GET action-link caveat. Navigating it fired nothing: it is the network's 'Your Privacy Choices' rights EXPLAINER, HTTP 200, about 6.8k of text, and it carries NO form and NO input of any kind. Its only routes off the page are the network do-not-sell page (courtrecords.us/do-not-sell-share-my- personal-information/), this row's own /do-not-sell-share-my- personal-information/ mirror, and email-only per-record removal to privacy@courtrecords.us. Identical to floridacourtrecords-us, georgiacourtrecords-us and hawaiicourtrecords-us; see californiacourtrecords-us for the full write-up. DATASET DEFECT, flagged and not fixed: the row records opt_out_method 'web-form' at indianacourtrecords.us/optout plus 'CAPTCHA required on the opt-out form', and there is no form on that page at all. One nuance worth recording against the earlier siblings' write-ups: this page DOES load Google's recaptcha api.js site-wide (cap[] reports it), but there is no captcha widget and nothing to submit -- the script is inert here, so the recorded CAPTCHA claim is still wrong, just not because reCAPTCHA is absent from the page's asset list.

### `info`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_OUT_OF_SCOPE`

  > OUT OF SCOPE, verified 2026-09-25 by rendering the URL on its own under the bare-GET action-link caveat (the hostname begins 'optout.'); navigating it fired nothing, it waits for selections. Read the search leg first: this row is a DATASET DEFECT whose domain field is the string 'info.' and whose company name was, by its own notes, 'inferred from domain'. Its recorded opt_out_url 'https://optout.aboutads.info.' -- stray trailing dot and all -- is the Digital Advertising Alliance's WebChoices tool, which belongs to the DAA and to no broker on this list. Rendered live it is HTTP 200, title 'WebChoices', and it describes itself as a way to 'control the collection and use of data for interest-based advertising ON THIS BROWSER': the visitor ticks boxes next to individual participating companies and presses 'Submit your choices', and the result is a set of per-browser opt-out cookies. That is out of scope three times over -- it is browser-scoped rather than record-scoped, it requires picking companies out of a list, and it is not this row's broker's own channel because this row has no identifiable broker. Nothing here can be automated on a person's behalf, and the row should be deleted or re-sourced rather than researched again.

- **scope:** dataset | **kind:** unclassified | **from:** `search_forms.NO_SEARCH_SURFACE`

  > DATASET DEFECT, verified 2026-09-25, and the leg is closed because the row identifies no broker. The row's domain field is the literal string 'info.' -- a trailing-dot fragment, not a domain -- which slugifies to the meaningless broker_id 'info' and will collide with any other row whose domain is mangled the same way. Its name field is 'Info', described in its own notes as 'company name inferred from domain'. Its recorded opt_out_url is 'https://optout.aboutads.info.' which, with the stray dot removed, is the Digital Advertising Alliance's WebChoices tool -- an industry-wide browser cookie utility, not a broker's site. There is therefore no company here whose search surface could be found, and nothing to render beyond the DAA tool itself (verified live, HTTP 200, title 'WebChoices'). Flagged, not fixed: this row should be deleted or re-sourced rather than researched further.

### `information-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NEXT STEP, concretely: fetch /privacy-rights/ and transcribe whatever it serves. Deliberately not doing that from the stale URL's redirect chain here, because the two buttons post rather than link, so what /privacy-rights/ shows may depend on which button was pressed -- and guessing which is how a recipe ends up filing a copy request when the user asked for deletion. DATASET NOTE: the source row's opt-out URL is wrong.

### `inmar-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_BLOCKED`

  > BLOCKED BY BOTDETECT, verified 2026-09-25 by browser render -- and the detector missed it again, exactly as it did on FinThrive in batch 26, so this is the second recorded instance of the same blind spot. DATASET DEFECT, flagged and not fixed: the row's opt_out_url is inmar.com/about/privacy-policy, a policy page with no request form on it (its three forms are all the Drupal site search), and inmar.com/do-not-sell-my-personal-information is a genuine HTTP 404. The policy's own 'EXERCISING YOUR PRIVACY RIGHTS' section gives two channels: calling 844-392-1073, and a link it labels simply 'webform' pointing at privacyportal.onetru st.com/webform/fa9f2f77-33ff-473b-ae55-579e2e693a91/b9c860bc- cc13-4db1-b995-395c9fcbe3c7. That form -- 'Resident Right to Access OR Deletion Request Form', scoped to residents of nineteen named states -- is real: required input#formField20DSARElement 'I am submitting this request as:', required input#firstNameDSARElement, required input#lastNameDSARElement, required input#emailDSARElement, required input#countryDSARElement, required input#stateDSARElement, a country-code input and a REQUIRED input#phoneNumberDSARElement, submitted by button#dsar-webform- submit-button. Its captcha is BotDetect, which loads no recognizable captcha script and so left cap[] and widget[] EMPTY on this render: the only tell is the field names inside the form -- hidden BDC_VCID_angularBasicCaptcha, BDC_BackWorkaround_..., BDC_Hs_..., BDC_SP_... plus a visible input[name=captchaCode] labelled 'Captcha'. A clean-looking cap[] on a OneTrust webform must never again be read as captcha-free. Also note the form offers only access and deletion -- no do-not-sell option -- and that the policy covers only inmar.com, pointing separately at an OwnerIQ notice (inmar.com/about/privacy-policy/owneriq) for the advertising business. privacy@inmar.com is published.

### `inmatessearcher-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_OUT_OF_SCOPE`

  > OUT OF SCOPE, verified 2026-09-25 by probing each URL on its own under the bare-GET action-link caveat; neither fired anything. TWO findings, and the first is a DATASET DEFECT: the recorded opt-out URL inmatessearcher.com/optOut/name/landing does NOT serve an opt-out form. It serves, byte for byte, the site's ordinary people-search landing page -- the same form#email-form asking a person's First Name, Last Name and state, the same 'FREE SEARCH' button, the same Cloudflare Turnstile frame -- because the Angular app falls through to the search route. Flagged, not fixed. The REAL opt-out entry point is the footer link 'Do Not Sell Or Share My Personal Info' (and its twin 'Limit The Use of My Sensitive Personal Info'), both pointing at https://www.inmatessearcher.com/api/helper/optOutLight/search. Rendered, that URL is a page titled 'Remove My Information' instructing 'Enter the name and state in the form below to LOCATE THE RECORD you would like to remove', and it holds form#pageForm POSTing to itself with required input[name=fname] 'First name', required input[name=lname] 'Last name', required input[name=city] 'City', required select[name=state] (51 options), optional input[name=zip], optional input[name=phone], optional input[name=email], a hidden input[name=captchaId], and input#pageFormSubmitBtn labelled 'SEARCH'. Out of scope for three stacked reasons, any one sufficient: (1) it is a RECORD- PICKING flow -- the first step searches, and the person must then identify their own listing out of the results, which this codebase will not do; (2) the hidden captchaId field means a captcha is wired into the submission path even where none rendered on step one, and the site's search side is behind Turnstile besides; (3) the dataset records an email confirmation link as required to finalize, an out-of-band hop. support@inmatessearcher.com is the published address; operator is Truth Now LLC, the same operator as sealedrecords.net.

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

### `iowacourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering https://iowacourtrecords.us/optout/ on its own, one target at a time under the bare-GET action-link caveat. Navigating it fired nothing: it is the network's 'Your Privacy Choices' rights EXPLAINER, HTTP 200, about 6.8k of text, and it carries NO form and NO input of any kind. Its only routes off the page are the network do-not-sell page (courtrecords.us/do-not-sell-share-my- personal-information/), this row's own /do-not-sell-share-my- personal-information/ mirror, and email-only per-record removal to privacy@courtrecords.us. Identical to floridacourtrecords-us, georgiacourtrecords-us and hawaiicourtrecords-us; see californiacourtrecords-us for the full write-up. DATASET DEFECT, flagged and not fixed: the row records opt_out_method 'web-form' at iowacourtrecords.us/optout plus 'CAPTCHA required on the opt- out form', and there is no form on that page at all. One nuance worth recording against the earlier siblings' write-ups: this page DOES load Google's recaptcha api.js site-wide (cap[] reports it), but there is no captcha widget and nothing to submit -- the script is inert here, so the recorded CAPTCHA claim is still wrong, just not because reCAPTCHA is absent from the page's asset list.

### `jdmlistservices-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: https://www.jdmlistservices.com/do-not-sell-my-info returns 404. DATASET NOTE: stale opt-out URL on a host that still answers. A list-services company is squarely a broker, so this is worth chasing rather than writing off -- the page presumably moved.

- **scope:** dataset | **kind:** unclassified | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: only the dataset's opt-out path was probed and it 404s on a live host. No search surface was looked for. DATASET NOTE: stale URL.

### `jigyasaanalytics-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_OUT_OF_SCOPE`

  > OUT OF SCOPE, verified 2026-09-25 by browser render. DATASET DEFECT, flagged and not fixed: the row's opt_out_url is jigyasaanalytics.com/contact-us, a contact page rather than the opt-out surface -- though unusually the page does name the real one. That page states 'Please note that Jigyasa Analytics LLC no longer operates as a data broker' and then 'To submit data deletion requests please fill out our Opt Out Form (California -CCPA, Texas, Vermont and all other states)', linking https://form.jotform.com/240995854286170. Rendered, that is a genuine multi-page JotForm titled 'Opt Out Form' posting to submit.jotform.com/submit/240995854286170, and it is transcribed in full: PAGE ONE is an identity gate -- input#input_15 (name=q15_pleaseAuthenticate) labelled 'Please authenticate your ID by entering your email addr[ess]', a 'Send Verification Code' button, a numeric code input, and 'RESEND CODE' / 'CHANGE EMAIL' buttons; subsequent pages, present in the DOM but off-layout until reached, ask required q2_fullName_1[first] and [last], q17_email, required q4_phone_3[full], and a required address block q5_address_4[addr_line1] / [addr_line2] / [city] / [state] / [postal], ending at a 'Review and Submit' button, with JotForm's usual jsExecutionTracker, submitSource, submitDate and buildDate hidden fields. No captcha script and no captcha widget were seen anywhere on it. It is out of scope for the reason the very first field states: the flow CANNOT PROCEED until a verification code emailed to the person is typed back into the form, an out-of-band, multi-step email-token exchange this codebase does not do. Worth flagging for a human pass: apart from that gate this form is clean, and a person driving it by hand would have no obstacle.

- **scope:** dataset | **kind:** unclassified | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-25 by browser render. jigyasaanalytics.com is an analytics consulting firm (synthetic data, publishing, financial services). The home page carries no form and no input at all. Its /contact-us page states, in its own words, 'Please note that Jigyasa Analytics LLC no longer operates as a data broker' -- recorded here as a broker-surface claim the dataset does not reflect, flagged and not fixed, since the company still publishes the opt-out form described on the other leg. Either way there is no consumer lookup surface.

### `kalibrate-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > BLOCKED BY BOTDETECT, verified 2026-09-25 by browser render -- the third instance of this detector blind spot recorded so far (FinThrive in batch 26, inmar-com in this batch), and the second where a OneTrust webform reported an EMPTY cap[] for the frame that actually holds the captcha. The recorded URL kalibrate.com/data-subject-access-request-form/ REDIRECTS to https://kalibrate.com/dsr-form/ -- a small dataset defect, flagged and not fixed. That page embeds a OneTrust DSAR webform in a child frame at privacyportal-uk.onetrust.com/webform/6e95a3 45-1222-4957-a118-93d60494951b/390b0718-0304-49af-9629- 7cb64a946019, and the parent page reports zero forms of its own apart from the site search, so a main-frame-only read would call this page empty. The embedded form is real and demanding: required input#countryDSARElement 'Country', required input#stateDSARElement 'State', required input#firstNameDSARElement, required input#lastNameDSARElement, required input#addressDSARElement 'Street Address', optional input#address2DSARElement, required input#cityDSARElement, required input#zipDSARElement 'Zip/Postal Code', required input#emailDSARElement, optional textarea#requestDetailsDSARElement, an input[type=file]#vt-file- select-input-1 'Select a File', and button#dsar-webform-submit- button 'Submit'. Its captcha is BotDetect, visible only as hidden BDC_VCID_angularBasicCaptcha / BDC_BackWorkaround_ / BDC_Hs_ / BDC_SP_angularBasicCaptcha plus input[name=captchaCode] labelled 'Captcha'. Separately, kalibrate.com runs reCAPTCHA v3 sitewide through Contact Form 7 (render key 6LcXQEQsAAAAAIGu1ZVeg26CG94g92ZZytsvD5iM) and leaves a textarea#g-recaptcha-response-100000 loose on the DSR page, so there are two independent bot checks in play.

### `kansascourtrecords-us`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering https://kansascourtrecords.us/optout/ on its own, one target at a time under the bare-GET action-link caveat. Navigating it fired nothing: it is the network's 'Your Privacy Choices' rights EXPLAINER, HTTP 200, about 6.8k of text, and it carries NO form and NO input of any kind. Its only routes off the page are the network do-not-sell page (courtrecords.us/do-not-sell-share-my- personal-information/), this row's own /do-not-sell-share-my- personal-information/ mirror, and email-only per-record removal to privacy@courtrecords.us. Identical to floridacourtrecords-us, georgiacourtrecords-us and hawaiicourtrecords-us; see californiacourtrecords-us for the full write-up. DATASET DEFECT, flagged and not fixed: the row records opt_out_method 'web-form' at kansascourtrecords.us/optout plus 'CAPTCHA required on the opt-out form', and there is no form on that page at all. One nuance worth recording against the earlier siblings' write-ups: this page DOES load Google's recaptcha api.js site-wide (cap[] reports it), but there is no captcha widget and nothing to submit -- the script is inert here, so the recorded CAPTCHA claim is still wrong, just not because reCAPTCHA is absent from the page's asset list.

### `kaspr-io`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > BLOCKED BY RECAPTCHA, verified 2026-09-25 by browser render, and it took a DELIBERATELY LONGER WAIT to see at all -- read the tooling note, it generalises. DATASET DEFECT, flagged and not fixed: the row records the privacy policy as the opt-out URL. The real surface, linked from both the home page and the policy as 'Do Not Sell My Information', is a Mine (saymine.io) privacy centre at https://kaspr.privacy.saymine.io/kaspr. TOOLING NOTE: at the prober's standard 2.5s settle this page reports HTTP 200 with document.body.innerText of length ZERO, no forms and no captcha -- indistinguishable from an empty page, and exactly the false-clean reading rule 1 exists to prevent. At 12s it is a full Angular DSR form (78kB of HTML). Titled 'Kaspr's Data Subject Request Form', it asks: Country (required, defaulted to United States); a required 'I would like to' choice of Right to edit / Get a copy of my data / Delete my data -- note there is NO do-not-sell or opt-out-of-sale option; then required First name, Last name, BUSINESS EMAIL, JOB TITLE, COMPANY NAME, BUSINESS MOBILE NUMBER and 'Your public LinkedIn link'; a required certification checkbox; a required relationship choice (Consumer / Employee / Authorized Agent); and Submit. The wall is a reCAPTCHA frame attached to the page (google.com/recaptcha/api2/anchor, sitekey 6LdpfrsaAAAAAKQQr6_BI1r_6-nn-FzTtVJU5t-L). Even past it this is the flashintel / B2B-profile shape already recorded elsewhere: job title, company and a public LinkedIn URL are all REQUIRED and resolve_fields has no source for any of them, so the identifier this broker files a person under is not one this codebase collects. privacy@kaspr.io with the subject 'Opt-out Kaspr' remains the published email channel.

### `kbmg-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: www.kbmg.com does not resolve (net::ERR_NAME_NOT_RESOLVED). UNREACHABLE for the defects list. KBM Group was a sizeable Wunderman/WPP data business, so a dead domain likely means absorption into a parent rather than closure -- which would mean the data still exists somewhere under another name. That is a research question, not something a DNS failure settles, and it is the kind of row the consolidated defects list exists to surface.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: www.kbmg.com does not resolve (ERR_NAME_NOT_RESOLVED). UNREACHABLE for the defects list; see optout_forms for why this one warrants a research pass rather than being written off as a dead domain.

### `keymarketingadvantage-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23: https://www.keymarketingadvantage.com/do_not_use_my_personal_information returns 404. DATASET NOTE: stale opt-out URL; the host still answers, so the path moved rather than the company vanishing.

- **scope:** dataset | **kind:** unclassified | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-23: only the dataset's opt-out path was probed and it 404s on a live host. DATASET NOTE: stale URL.

### `keyopinionleaders-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24: www.keyopinionleaders.com does not resolve (net::ERR_NAME_NOT_RESOLVED from a real browser). DNS returns nothing for the name, so there is no host to ask and nothing can be said about any opt-out surface. UNREACHABLE for the defects list.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-24: www.keyopinionleaders.com does not resolve (net::ERR_NAME_NOT_RESOLVED), so there is no host to ask. UNREACHABLE for the defects list.

### `klarifi-io`

- **scope:** broker-surface | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Incidental page defect worth recording since it would break a naive selector: the message <textarea> carries id='name', DUPLICATING the id of the name <input>. A recipe keyed on #name would match two elements of different kinds. A DEFECT ON THE PAGE ITSELF, not a dataset problem.

### `l2political-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24, with a DATASET DEFECT recorded and not fixed: the opt_out_url on file -- l2political.com/california-privacy-rights-for-california-residents-only/ -- returns a hard HTTP 404, and so does the conventional l2political.com/privacy-policy/ tried as a fallback. The domain itself resolves and serves, so this is a dead path on a live site rather than a dead company. The real rights page was not located this pass; finding it is a five-minute job for whoever picks this up, starting from the site's own footer.

### `leadershipconnect-io`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24: HTTP 403. Both the dataset's recorded path and www.leadershipconnect.io/opt-out answered 403 with no content, so nothing is known about whether an opt-out form exists there. UNREACHABLE for the defects list.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-24: leadershipconnect.io answers HTTP 403 with no content, on both the dataset path and /opt-out, so neither leg could be read. UNREACHABLE for the defects list. Likeliest cause is an edge rule refusing a headless datacenter client; recheck from the deployment host, as with thatsthem-com.

### `leadloft-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > WHAT WAS ACTUALLY CHECKED, since this is an absence claim and the standing rule is that absence must be established rather than merely unobserved. (1) www.leadloft.com was rendered in full. Its anchors were scanned for any text or href mentioning do-not-sell, opt-out, privacy choices, remove, request, DSAR or suppress: there are NONE. Not a dead link, not an empty href as at hivestack-com -- no such anchor exists on the page. (2) The two conventional paths, /privacy-policy and /do-not-sell, were each requested and each returns a hard HTTP 404.

### `leadsmarket-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24: www.leadsmarket.com/privacy-policy did not respond. Navigation to it was attempted twice and both attempts ended in a 30-second timeout with no response at all -- no challenge page, no error page, nothing, the same signature already recorded at trufactor-io. UNREACHABLE for the defects list.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-24: www.leadsmarket.com did not respond -- two attempts, both 30-second navigation timeouts with no response at all. Nothing can be said about any surface. UNREACHABLE for the defects list; see the optout_forms entry for why two timeouts from one network still are not proof the host is gone.

### `leadspace-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24, and it is a close call held up by the same wrong-request-type problem already recorded at gumgum-com.

### `leadzod-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > Verified by browser render (Playwright, 11-13s settle) 2026-09-25. RECIPE-READY IN SUBSTANCE, held back only by Wix's form transport. The dataset records opt_out_url as null and opt_out_method 'email', which is FALSIFIED: www.leadzod.com/your-privacy-choices serves a real, captcha-free rights form (page heading 'Your Privacy Choices Contact Us Form'), linked from the footer of every page as 'Your Privacy Choices'. Full transcription, element by element -- form id 'form-3537423c-e438-410a-b550-e488e7c78d20', method GET, action back to the same page: input[type=text] REQUIRED labelled 'First name*', id 'form-field- input-7e9d7a34-8f8d-481e-ee07-66a36d1ef4ed-comp-lz1mqa5d-'; input[type=text] optional labelled 'Last name', id 'form-field- input-4f006c2a-1363-4911-5f72-020aa087e055-comp-lz1mqa5d-'; input[type=email] REQUIRED labelled 'Email*', id 'form-field- input-8ffeec5e-d1fd-49d4-b311-bf77a33d3eea-comp-lz1mqa5d-'; textarea optional labelled 'Write a message', id 'form-field- input-8aeac2cb-e4f2-4a80-9a92-21d659b14f99-comp-lz1mqa5d-'; button 'Submit'. cap[] and widget[] are both EMPTY at an 11s settle -- no reCAPTCHA, no Turnstile, no honeypot flagged invisible, no email-confirmation step advertised. WHY IT IS NOT A RECIPE. Not one control has a name attribute; Wix identifies fields by those opaque comp- ids and submits them through its own _api endpoint rather than by POSTing the form's action, so a recipe keyed on field names cannot be written and a recipe keyed on those ids would break the moment the page is re-published in the Wix editor. The 'opt-out' framing is also the site's own: this is a contact form repurposed as the privacy channel, so the request text has to go in the message textarea. Also worth recording for the next pass: the dataset note says LeadZod replied on 2026-08-21 that they are a service provider holding no personal information of their own, and the homepage tells California mobile users to 'see Privacy Choices link in mobile menu for privacy requests' -- so this form is the intended channel even though the company disclaims holding data.

### `limeleads-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24, with a DATASET DEFECT that is worth distinguishing from an ordinary 404. Both the recorded opt-out URL (/do-not-sell-my-data-request/) and the bare apex return WP Engine's 'Site Not Configured' page: 'This domain is successfully pointed at WP Engine, but is not configured for an account on our platform.'

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-24: limeleads.com serves WP Engine's 'Site Not Configured' page on every path tried -- the apex and the recorded opt-out path alike -- so no surface of any kind was observed and none can be described. UNREACHABLE for the defects list; see optout_forms for why a hosting-level 'not configured' is a different animal from a 404 and from a DNS failure.

### `lizdev-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-24: lizdev.com answered HTTP 403 from Cloudflare ('Sorry, you have been blocked ... You are unable to access secureservercdn2.net') and no content was retrieved, so neither leg could be read. UNREACHABLE for the defects list.

### `localblox-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > The recorded opt-out host, consumer.localblox.com, does not resolve (net::ERR_NAME_NOT_RESOLVED) -- the SUBDOMAIN is gone while the apex is not. localblox.com itself answers 200 and serves an empty default WordPress installation: a title that is just the domain name, a stock nav of Home / About / Services / Blog / Shop / Contact, a single 'Home' heading, a search box, and no content under any of it. That is a placeholder someone stood up on the name, not a company website.

### `locatesmarter-com`

- **scope:** dataset | **kind:** rebrand-or-domain-change | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-24, after a DATASET DEFECT that would have stopped a mechanical retry dead. The recorded opt_out_url on www.locatesmarter.com returns HTTP 404; the apex now redirects to portal.locatesmarter.com, which announces 'Our website is currently under maintenance' and carries nothing but links. Two of those links are the live surfaces, on a THIRD host: form.locatesmarter.com. Note the path is case-sensitive and capitalises the last word -- '...my-personal-Information' -- which is the kind of thing that turns a working URL into a 404 when it is retyped. Flagged, not fixed.

### `logiq-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-24: logiq.com is a PARKED DOMAIN LISTED FOR SALE on GoDaddy Auctions. The page is GoDaddy's aftermarket shell -- 'logiq.com is available on GoDaddy Auctions', a 'Get This Domain' button, and keyword-ad filler for unrelated products (Logiq E Ultrasound, Logiq Air Suspension, Logiq Coffee). There is no site behind the name.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > This CORROBORATES what the dataset already says -- its notes record that Logiq Inc (LGIQ) is delisted and that the site was non-functional at an earlier check -- and it is the second for-sale listing in the module after idengine-com. The row records no opt-out URL, no opt-out email and an unknown method, so there was never a channel here to lose.

### `lotadata-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-24: lotadata.com does not resolve (net::ERR_NAME_NOT_RESOLVED), and unlike most dead-domain rows this one is filed as ABSENCE rather than as an open question, because the company told us itself.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified 2026-09-24: lotadata.com does not resolve, and the company stated to us directly that it is no longer operational. The leg is closed on the same basis as the opt-out leg; see optout_forms for why this row is filed as absence rather than as an open question.

### `m1-data-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24: HTTP 403 from Cloudflare. m1-data.com/unsubscribe/ returned the block page 'Sorry, you have been blocked ... You are unable to access secureservercdn2.net'. No content was retrieved. UNREACHABLE for the defects list.

### `mailinglists-com`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > BLOCKED BY INVISIBLE RECAPTCHA ENTERPRISE, verified 2026-09-25 by browser render. DATASET DEFECT, flagged and not fixed: the row's opt_out_url is the company's general privacy-policy page. The actual surface is a footer link reading 'DO NOT SELL MY PERSONAL INFORMATION FORM' on the home page, pointing at a shared HubSpot form -- 41b1vr.share-na2.hsforms.com/2mF48cI- GSjaSTP9FG8gkjA -- and that form is real and well-scoped for this codebase: required input[name='0-1/firstname'] 'First Name', required input[name='0-1/lastname'], required input[type=email][name='0-1/email'], optional input[name='0-1/address'] 'Street Address', '0-1/city', '0-1/state' 'State/Region', '0-1/country', a tel field backed by hidden input[name='0-1/phone'], two name='0-1/consumer_privacy_request_type' radios (the visible page labels the first 'Do Not Sell...'), and a REQUIRED checkbox name='0-1/consumer_privacy_request_confirmation' 'I confirm that I am submitting this request for myself'; submit is button id ...-15 'Submit', with hidden hs_context. Element ids are per- render GUIDs prefixed 9c9cc5b8-8281-42f6-a407-2f61c7c18288, so a driver must key on name, not id. The wall: google.com/recaptcha/enterprise.js (sitekey 6Lfsit8ZAAAAAKdtNnFH8HrpgF-JzgzfjHlxxNVK, badge=inline), a grecaptcha-badge, BOTH textarea[name=g-recaptcha-response] and hidden input#hs-recaptcha-response inside the form, and the enterprise anchor and bframe frames attached. This is the growinglibraries-com shape again: a complete, honest, fully transcribed form whose only obstacle is an INVISIBLE enterprise captcha with no visible widget. Promote it the day this codebase can answer one.

### `marketops-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24: net::ERR_CONNECTION_REFUSED on both the recorded /privacy-options/ path and the bare apex. UNREACHABLE for the defects list.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > Refused is not the same as timed out and not the same as a DNS failure: the name resolved and something answered at the TCP layer with a rejection, which is consistent with a decommissioned service on a live name, with an origin that is down, or with an edge that drops non-browser clients. It does not establish that the company is gone. See optout_forms for the same note.

### `matchandappend-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24: two 30-second navigation timeouts, on the recorded /do-not-sell-my-data/ path and on the bare apex, with no response of any kind. UNREACHABLE for the defects list.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-24. The request timed out on two separate attempts -- no response at all, not a rejection. Nothing about a search surface was observed.

### `mediasoftstudio-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified by browser render (Playwright, 11-13s settle) 2026-09-25. mediasoftstudio.com IS DEAD, in three independent ways. (1) HTTPS is broken: Playwright refused the navigation with net::ERR_CERT_DATE_INVALID and curl reports 'certificate has expired', so nothing on the site can be reached over TLS at all. (2) Over plain HTTP the root serves a 17-character body reading 'Under Maintenance' (title 'Under Maintenance') and nothing else -- no forms, no links, no captcha. (3) The dataset's opt_out_url path, /unsubscribe.php, returns HTTP 404 over HTTP. Probed as a single target on its own invocation, since the URL contains 'unsubscribe'. Dataset claim falsified: opt_out_method 'web-form' at www.mediasoftstudio.com/unsubscribe.php is a 404 behind an expired certificate. Nothing remains but info@mediasoftstudio.com, on a domain whose web presence is a maintenance stub -- no reachable form, hence no-surface rather than blocked (an expired cert and a 404 are the finding, not an anti-bot wall).

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified by browser render (Playwright, 11-13s settle) 2026-09-25. mediasoftstudio.com IS DEAD, in three independent ways. (1) HTTPS is broken: Playwright refused the navigation with net::ERR_CERT_DATE_INVALID and curl reports 'certificate has expired', so nothing on the site can be reached over TLS at all. (2) Over plain HTTP the root serves a 17-character body reading 'Under Maintenance' (title 'Under Maintenance') and nothing else -- no forms, no links, no captcha. (3) The dataset's opt_out_url path, /unsubscribe.php, returns HTTP 404 over HTTP. Probed as a single target on its own invocation, since the URL contains 'unsubscribe'. No search surface: there is no site left to search.

### `mediasourcesolutions-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified by browser render (Playwright, 11-13s settle) 2026-09-25. www.mediasourcesolutions.com IS GONE. Every path on it, including the bare domain and the do-not-share URL from the notes, 302s to /cgi-sys/suspendedpage.cgi and serves a 103-character body reading 'Account Suspended / This Account has been suspended. / Contact your hosting provider for more information.' -- the cPanel suspension page. Probed twice in the same batch (root and the rights URL) with the same result. This is a dead site, not an anti-bot wall: status 200, no captcha, no challenge, no frames, nothing to render. The dataset's 2026-09-12 sweep note claims 'their own privacy page lists privacy@mediasourcesolutions.com and a dedicated do-not-share form' at www.mediasourcesolutions.com/do-not-share-my-personal- information/. That claim is now FALSIFIED: the URL is suspended along with the rest of the host, so there is no form, and the privacy page it cites is unreachable too. Consistent with the earlier recorded finding that mail to the domain hard-bounced on 2026-08-21 -- the email channel and the web channel are both dead, which is why this is no-surface rather than undecided.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.NO_SEARCH_SURFACE`

  > Verified by browser render (Playwright, 11-13s settle) 2026-09-25. www.mediasourcesolutions.com IS GONE. Every path on it, including the bare domain and the do-not-share URL from the notes, 302s to /cgi-sys/suspendedpage.cgi and serves a 103-character body reading 'Account Suspended / This Account has been suspended. / Contact your hosting provider for more information.' -- the cPanel suspension page. Probed twice in the same batch (root and the rights URL) with the same result. This is a dead site, not an anti-bot wall: status 200, no captcha, no challenge, no frames, nothing to render. No search surface can exist on a suspended host.

### `netwisedata-com`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-25 by rendering netwisedata.com. DATASET DEFECT: the recorded opt_out_url https://www.netwisedata.com/consumer-privacy returns HTTP 404 with an empty body, with and without a trailing slash -- there is no page there and therefore no form on it. NetWise has been folded into Dun & Bradstreet: the apex now lands on https://www.dnb.com/en-us/products/dnb-id-graph-plus.html, the identity-graph product built on that data, and the only rights route on it is D&B's own footer 'Your Privacy Choices' pointing at the TrustArc form at submit- irm.trustarc.eu/services/validation/ba81b98f-... . That form is BLOCKED by invisible reCAPTCHA v3 (api.js?render=6LeUJoQa..., grecaptcha-badge, g-recaptcha-response), written up in full on the dnb-com and trustarc-eu rows. So the correct reading of this row today is: the NetWise-branded surface is gone, the successor surface exists and is challenged. privacy@netwisedata.com is recorded on the row and is the only channel that does not require solving a captcha; whether that mailbox still answers after the merge was not tested.

### `nuwber-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and for an unusual reason worth recording rather than retrying blindly: nuwber.com would not RESOLVE. A headless Chromium navigation to the dataset's opt_out_url (nuwber.com/removal/link) failed with net::ERR_NAME_NOT_RESOLVED -- a DNS failure, not a timeout, a certificate problem or an anti-bot block, and distinct from the HTTP errors recorded on other rows here. One observation from one host on one day is not enough to call a large and previously-active people-search site dead, so this is undecided rather than no-surface. Next pass: resolve the name from a different network before concluding anything, and if it resolves, render /removal/link and transcribe. Both legs of this broker are unresolved for the same reason.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT as of 2026-09-23 for a network reason, not a research one: nuwber.com failed to RESOLVE (net::ERR_NAME_NOT_RESOLVED) from a headless Chromium on this host, so no page was ever reached. Nuwber is a well-known people-search site, so a single DNS failure is not grounds for a no-surface call in either leg. Next pass: resolve the name from a different network first. See the opt-out leg's entry, which is unresolved for the same reason.

### `outlastdfs-com`

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-24: outlastdfs.com does not resolve (net::ERR_NAME_NOT_RESOLVED). UNREACHABLE for the defects list.

- **scope:** unreachable | **kind:** parked-or-defunct | **from:** `search_forms.SEARCH_UNDECIDED`

  > NO VERDICT 2026-09-24: outlastdfs.com does not resolve (net::ERR_NAME_NOT_RESOLVED). UNREACHABLE for the defects list, and corroborated independently by the hard-bounced mail recorded in the dataset a month earlier -- see optout_forms for why this is still filed as undecided rather than as absence.

### `parade-pet`

- **scope:** dataset | **kind:** dead-url | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and the surface was found only because the site leaks it. parade.pet is a single-page app: every path, including ones that return HTTP 404, serves the same shell, and that shell contains every form the app will ever show. Enumerating them turns up signUpForm, loginForm, phoneNumberForm, smsCode, emailCodeForm -- and, decisively, form#optOutLoginForm, an email box with a Login button. So an opt-out flow exists and is reachable, which the visible site never advertises; the homepage FAQ item 'How do I delete my account and remove my ...' links only to an on-page accordion, and an 'Opt out of marketing' link points at '#'. It is undecided because the flow is GATED ON AN EMAILED CODE. optOutLoginForm takes an email and logs you in; emailCodeForm then asks for a code delivered out of band. Nothing beyond that step was observed, so the fields that carry the actual request are unknown. Same shape as forager-ai in the previous batch and as the shipped ADVANCEDBACKGROUNDCHECKS recipe, so not out of scope in principle. One caution for whoever continues: because the SPA serves all forms at all times, PRESENCE OF A FORM IN THE DOM DOES NOT MEAN IT IS ON SCREEN. A recipe here must assert the opt-out view is actually displayed before filling anything, or it will type into a hidden login box and report success. DATASET NOTE, flagged not fixed: this row's domain is parade.pet but its contact is hello@goodboystudios.com -- the operator's name, not the site's.

### `perion-com`

- **scope:** broker-surface | **kind:** broker-surface-defect | **from:** `optout_forms.OPTOUT_OUT_OF_SCOPE`

  > OUT OF SCOPE, verified 2026-09-25 by browser render, for two independent reasons -- and with a broker-surface defect worth reporting on its own. FIRST, the only actuator Perion publishes is a PER-BROWSER COOKIE. perion.com/opt-out/ ('OPT-OUT TOOL') states it plainly: 'Perion offers an opt-out cookie for those that wish to opt-out of Perion IBA activities... If you have multiple Internet browsers or users on the same computer, you will need to perform the opt-out operation for each browser... If you or your anti-virus or other software deletes the opt-out cookies from your computer, if you re-install your browser, or if you delete your cookies, then you will need to repeat the process.' There is no form on that page at all -- the page's only controls are the Cookiebot consent dialog and a 'Click here to Opt-Out' actuator -- and it also routes to the NAI and DAA industry tools and to tools.google.com/dlpage/gaoptout. A cookie set in a throwaway automation browser opts out nothing that belongs to the person. SECOND, the identifier: perion.com/ccpa/ says Perion files people under Mobile Advertising IDs and that 'we often receive requests from users identifying themselves via an e-mail address, name or other direct identifiers, however as our offerings do not include the collection or processing of this type of information (rather, device identifiers such as MAIDs) we are not able to identify the user' -- the same MAID- keyed dead end already recorded for complementics, collectivedata, datafy and factori. THIRD, THE DEFECT: the CCPA notice tells the reader eight times to 'submit the DSR' or 'fill in the DSR', and NO DSR FORM IS LINKED ANYWHERE. Every anchor on /ccpa/, /legal/ and /opt-out/ was enumerated; the only thing resembling one is an UNREPLACED COOKIEBOT TEMPLATE PLACEHOLDER, a link whose text is the literal '[#DSR_FORM_URL_TEXT#]' pointing at 'https://perion.com/ccpa/[#DSR_FORM_URL#]' (and the same broken link again on /legal/ and /opt-out/), alongside a literal '[#IABV2SETTINGS#]'. This is the identical misconfiguration already recorded for illumin / acuityads -- same vendor, same unset variable -- which makes it a Cookiebot- deployment pattern worth checking for on every Cookiebot site in the dataset, not a one-off. The data-broker registrant is the affiliate Hivestack Inc.; the notice's own metrics table reports zero requests of every kind received from Californians, which is what an unlinked DSR form would produce. Shine-the-Light requests go to an email address the notice gives in prose.

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

### `socialgist-ai`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.NO_OPTOUT_SURFACE`

  > Verified 2026-09-25 by rendering socialgist.ai and its /privacy- and-terms in full. There is no opt-out form. The policy is a long, genuinely detailed US-state rights notice -- right to know, delete, correct, opt out of sales, non-discrimination, for residents of California and a dozen other states -- and every one of those rights routes to a mailbox, privacy@socialgist.com (info@socialgist.com is the general address; note both are on socialgist.COM while this row's id comes from the .ai domain). The only link on the entire privacy page is Google's own analytics opt-out at tools.google.com/dlpage/gaoptout, which is not a Socialgist channel. Mailbox-only, hence this bucket -- same call as crif-com. DATASET DEFECT: the row records opt_out_method 'web-form' at this URL and no opt_out_email; there is no form here, and the published address should be on the row. One thing that materially shapes what a request can even ask for, and the reason a name-and-address recipe would not fit this broker anyway: Socialgist holds personal information only as it appears inside collected social Content, keyed to handles and usernames, and the policy says so -- it will not process requests that do not let it link the request to applicable records in the Content, i.e. it wants the handles, and it verifies identity before acting.

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

### `trustarc-eu`

- **scope:** dataset | **kind:** unclassified | **from:** `optout_forms.OPTOUT_BLOCKED`

  > Verified 2026-09-25 by rendering the URL this row records, which resolves and is the real surface: submit-irm.trustarc.eu/service s/validation/ba81b98f-997d-4216-b4cc-d64cf261b082 renders 'Data Subject Request Form' -- correct for the row, since the broker here is Dun & Bradstreet and TrustArc merely hosts the form (the same URL is what dnb.com and the absorbed netwisedata.com both link as 'Your Privacy Choices'). The form is complete: three required single-line inputs, each shadowed by a hidden twin of the same GUID name, data-category checkboxes 'Consumer Data' / 'Business Data' / 'Professional Contact Data', an optional 'Eyeota Cookie ID (optional, see details below)', a button#add- more-single-line-btn repeater and further GUID-named inputs. BLOCKED by INVISIBLE reCAPTCHA v3: google.com/recaptcha/api.js?r ender=6LeUJoQaAAAAAAAYuHIlzgY0JwxfTErqtLAzBwBD is loaded and grecaptcha-badge, grecaptcha-logo, grecaptcha-error and g-recaptcha-response are all in the DOM -- nothing a human visitor sees, and everything a recipe would fail on. Two structural notes for whoever revisits: every control is addressed by GUID (id='00000000-0000-0000-0000-000000001001', name='bb2c6098-...'), some of which are TrustArc's stable well- known ids and some per-deployment, so selectors would have to be verified against a fresh render; and the DATASET NOTE on this row is wrong in a harmless direction -- it calls the URL a 'general privacy-policy page (not necessarily a dedicated consumer opt-out form)' when it is in fact the dedicated form. The EU DPO address eudpo@dnb.com on the dnb-com row is the mailbox alternative.

### `winwithoptimal-com`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, because the company behind this row has been renamed and the checklist is keyed to the old domain. DATASET DEFECT, flagged here and deliberately NOT fixed in data/source-brokers.json: the row is 'Dspolitical, LLC' at winwithoptimal.com with info@dspolitical.com. Requesting winwithoptimal.com/privacy-policy/ redirects to https://www.onemagnify.com/privacy-policy -- DSPolitical / Optimal is now OneMagnify. The old domain still serves its own marketing pages, so this is a live rebrand mid-flight rather than a dead domain, which is why the redirect only shows up on the policy path. What the recorded opt_out_url actually is: /opt-out-of-advertising/ is an EXPLAINER about managing cookies. Its only first-party form is #footerForm, a single-email Gravity newsletter box (gf_field_3_1) -- a marketing signup, not an opt- out, and a careless reader could easily write a recipe against it. The opt-out links it does give are all third-party: Google's gaoptout, adsrvr.org, Yahoo's device dashboard. Cookie consent is OneTrust. Next step for whoever picks this up: research OneMagnify, not winwithoptimal. A first pass over onemagnify.com/privacy-policy found no anchor matching opt-out / do-not-sell / request / rights at all, so the rights channel there is probably a mailto or a portal named something else in the policy prose, and the policy text needs reading rather than its links scanning. If the surface is found, consider whether this row should be re-keyed to onemagnify-com.

### `worldpay-com`

- **scope:** dataset | **kind:** entity-mismatch | **from:** `optout_forms.OPTOUT_UNDECIDED`

  > NO VERDICT as of 2026-09-23, and the reason is a tangle in the dataset row rather than anything the broker has done. DATASET DEFECT, flagged and NOT fixed: the row is named 'Efunds Corporation', keyed to worldpay.com, with chexsystems.compliance@fisglobal.com as the contact. Those are three different things. eFunds is the company behind CHEXSYSTEMS, the banking consumer reporting agency that decides whether someone can open a checking account. Worldpay is a payments processor. Both passed through FIS ownership, which is how they came to share a row, but the consumer-facing reporting product is not on worldpay.com at all -- and ChexSystems, like earlywarning-com in this module, is an FCRA agency whose file is not something a consumer can opt out of. What was actually found at the recorded domain: worldpay.com carries 'Do not sell or share my personal information' pointing at privacy.worldpay.com, which resolves to /policies and is a Transcend-powered privacy centre ('Powered by Transcend') offering 'Make a Privacy Request' and 'View Past Requests'. It is a single-page app -- /request 404s WITHIN it, so the request flow opens from the button rather than from a URL, and nothing could be transcribed without driving it. Next steps, in order, because they are two different jobs: (1) drive the Transcend portal from the button, enumerate the request-type options and the fields, and re-check for a captcha at that stage -- none is loaded on the landing page, which proves nothing; (2) decide whether this row should be re-keyed to chexsystems.com, and if so whether it belongs with earlywarning-com as an FCRA agency with no opt-out rather than here.

