"""Run the REAL detection legs against exactly ONE broker, on demand.

Why this exists
---------------
A full sweep is ``brokers x profiles`` and takes hours, and the dashboard
only ever shows the *collapsed* answer: a per-broker ``error`` tally with
no way to ask "error because of WHAT?". When a broker starts accumulating
errors, the only two options used to be "wait for the next multi-hour
cycle and squint at a counter" or "read possibly-stale stored state".
This module is the third option: point it at one broker and it runs the
exact same per-(broker, identity) checks the production sweep runs, for
every identity a real scan would cover, and prints everything it saw.

Three properties make it a diagnostic rather than a second sweep:

* **Same code path.** It calls ``sweep._check_serp`` and
  ``sweep._check_browser`` -- the very functions ``sweep._check_pair``
  calls -- rather than reimplementing the query building, domain scoping
  or check normalization. If detection changes, this changes with it.
  It deliberately does NOT call ``_check_pair``: that is the function
  that records to ``progress``/``state.sqlite``.
* **Read-only.** No ``StateStore`` is opened, ``progress.record_outcome``
  is never called, and nothing the dashboard reads is touched. Running
  this cannot change what the dashboard shows.
* **Nothing is swallowed.** Both production legs are deliberately
  resilient: ``serpwatch.run_serpwatch`` catches each query's exception
  and reports only the exception *class* (never ``str(exc)``, which could
  carry the queried PII), and ``playwright_checks.run_playwright_checks``
  collapses a raising ``page_action`` into an ``{"error": "..."}`` dict.
  That is correct for a sweep and useless for a diagnosis. So the two
  backend callables are wrapped here in recording proxies that capture
  the full traceback and then **re-raise**, leaving the production
  handling byte-for-byte unchanged while the tracebacks are printed
  afterwards. "The check failed, here is exactly why" is the whole point.

PII note: this prints identity detail (name variants, emails, phones) to
stdout for a human running it interactively. That is fine -- it is not the
shared, PII-redacted log stream that ``logging_setup`` governs. Nothing
here writes to the log files.
"""
import traceback

from broker_guard import brokers as brokers_mod
from broker_guard import profile as profile_mod
from broker_guard import profiles as profiles_mod
from broker_guard import service as service_mod
from broker_guard import sweep as sweep_mod


class BrokerLookupError(Exception):
    """No broker, or more than one broker, matched the query.

    ``candidates`` is the ambiguous match list (empty for "no match"), so
    the caller can print it instead of the tool picking one at random.
    """

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = list(candidates or [])


def find_broker(broker_list, query):
    """The single broker *query* names, or raise ``BrokerLookupError``.

    Matching is tried most-specific-first so an exact answer is never lost
    to a substring one: exact id, then exact name, then case-insensitive
    substring over both. An ambiguous substring match refuses and reports
    the candidates rather than guessing -- picking one silently is how a
    diagnosis ends up describing a broker you were not asking about.
    """
    wanted = (query or "").strip().lower()
    if not wanted:
        raise BrokerLookupError("empty broker query")

    exact_id = [b for b in broker_list if str(b.get("id", "")).lower() == wanted]
    if len(exact_id) == 1:
        return exact_id[0]

    exact_name = [b for b in broker_list
                  if str(b.get("name", "")).strip().lower() == wanted]
    if len(exact_name) == 1:
        return exact_name[0]

    partial = [b for b in broker_list
               if wanted in str(b.get("name", "")).lower()
               or wanted in str(b.get("id", "")).lower()]
    if not partial:
        raise BrokerLookupError(
            "no broker matches {!r} ({} brokers loaded)".format(query, len(broker_list)))
    if len(partial) > 1:
        raise BrokerLookupError(
            "{!r} is ambiguous: {} brokers match".format(query, len(partial)),
            candidates=partial,
        )
    return partial[0]


class _Recorder:
    """Wrap a backend callable so raised exceptions are captured WITHOUT
    being intercepted: the traceback is recorded and the exception is then
    re-raised, so the production handler downstream behaves exactly as it
    does in a real sweep."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.failures = []   # list[str] -- one formatted traceback per raise

    def __call__(self, *args, **kwargs):
        self.calls += 1
        try:
            return self.inner(*args, **kwargs)
        except BaseException:
            self.failures.append(traceback.format_exc())
            raise


def _wrap_deps(deps):
    """A ``Dependencies`` twin of *deps* with both legs' backends recorded.

    Returns ``(wrapped_deps, searx_recorder, page_recorder)``. A leg that
    is switched off stays ``None`` (and its recorder is ``None`` too), so
    the "this leg had no opinion" path is reported honestly instead of
    looking like a leg that ran and found nothing.
    """
    searx = _Recorder(deps.searx_search) if deps.searx_search is not None else None
    page = _Recorder(deps.page_action) if deps.page_action is not None else None
    wrapped = service_mod.Dependencies(
        searx_search=searx,
        page_action=page,
        store=None,          # read-only: never opens state.sqlite
    )
    return wrapped, searx, page


def _describe_identity(identity):
    return {
        "identity_key": identity.identity_key,
        "full_name": identity.full_name,
        "name_variants": profile_mod.name_variants(identity),
        "emails": list(identity.emails),
        "phones": list(identity.phones),
        "addresses": list(identity.addresses),
    }


def diagnose_broker(broker, identities, deps, write=print) -> int:
    """Run both production legs for *broker* against every identity.

    Returns a process exit code: 0 when every leg completed (even if the
    answer was "absent"), 1 when anything errored -- so this is usable
    from a script as well as by eye.
    """
    failures = 0

    write("broker: {} [{}]".format(broker.get("name"), broker.get("id")))
    write("  url:          {}".format(broker.get("url")))
    write("  verification: {}".format(brokers_mod.verification_kind(broker)))
    write("  automatable:  {}".format(
        brokers_mod.is_automatable(brokers_mod.verification_kind(broker))))
    write("  identities:   {}".format(len(identities)))
    write("")

    if not identities:
        write("NO IDENTITIES TO SCAN -- a real sweep would check nothing here.")
        write("Check BG_PROFILES_PATH / BG_PROFILE_PATH.")
        return 1

    for index, identity in enumerate(identities, start=1):
        info = _describe_identity(identity)
        write("--- identity {}/{}: {} ({}) ---".format(
            index, len(identities), info["full_name"], info["identity_key"]))
        write("    name variants: {}".format(info["name_variants"]))
        write("    emails:        {}".format(info["emails"]))
        write("    phones:        {}".format(info["phones"]))
        write("    addresses:     {}".format(info["addresses"]))

        probe, searx_rec, page_rec = _wrap_deps(deps)

        # --- SERP leg -------------------------------------------------
        if probe.searx_search is None:
            write("    SERP leg:    DISABLED (no searx_search configured)")
        else:
            try:
                serp_outcome, serp_hits = sweep_mod._check_serp(broker, identity, probe)
                write("    SERP leg:    outcome={!r} hits={} queries={}".format(
                    serp_outcome, serp_hits, searx_rec.calls))
                if serp_outcome == "error":
                    failures += 1
            except Exception:
                failures += 1
                write("    SERP leg:    RAISED (the production sweep would have"
                      " swallowed this)")
                write(_indent(traceback.format_exc()))
            for tb, count in _dedupe(searx_rec.failures):
                failures += 1
                write("    SERP backend exception{} (swallowed by"
                      " run_serpwatch, shown in full here):".format(
                          "" if count == 1 else " x{}".format(count)))
                write(_indent(tb))

        # --- browser leg ----------------------------------------------
        if probe.page_action is None:
            write("    browser leg: DISABLED (BG_PLAYWRIGHT_ENABLED off)")
        else:
            try:
                result = sweep_mod._check_browser(broker, identity, probe)
                if result is None:
                    write("    browser leg: NO OPINION (broker is not automatable,"
                          " or its URL is not an http(s) URL)")
                else:
                    write("    browser leg: {!r}".format(result))
                    if not result.get("checked"):
                        failures += 1
            except Exception:
                failures += 1
                write("    browser leg: RAISED (the production sweep would have"
                      " swallowed this)")
                write(_indent(traceback.format_exc()))
            for tb, count in _dedupe(page_rec.failures):
                failures += 1
                write("    browser backend exception{} (swallowed by"
                      " run_playwright_checks, shown in full here):".format(
                          "" if count == 1 else " x{}".format(count)))
                write(_indent(tb))
        write("")

    write("{} problem(s) surfaced.".format(failures) if failures
          else "Both legs completed for every identity with no errors.")
    return 1 if failures else 0


def _dedupe(tracebacks):
    """``[(traceback, occurrences)]``, first-seen order.

    A broker is queried once per name/email/phone term, so one dead
    SearXNG produces the same 40-line traceback N times. Collapsing the
    identical ones keeps the output readable WITHOUT losing the fact that
    it happened more than once -- the count is printed.
    """
    counts = {}
    for tb in tracebacks:
        counts[tb] = counts.get(tb, 0) + 1
    return list(counts.items())


def _indent(text, prefix="      | "):
    return "\n".join(prefix + line for line in text.rstrip("\n").splitlines())


def run(cfg, query, write=print) -> int:
    """Entry point: resolve *query* against ``cfg.brokers_path`` and
    diagnose it for every identity a real scan would sweep.

    Builds only the detection layer (``service.build_detection``) -- the
    same construction the autopilot's ``_detection_deps`` uses -- rather
    than ``build_dependencies``, because a one-shot check has no use for a
    state store, an alert sink or a removal bridge, and opening the state
    store is precisely the side effect this tool must not have. The layer's
    closers run in a ``finally`` so an enabled Playwright browser is never
    leaked, even when the diagnosis blows up.
    """
    broker_list = brokers_mod.load_brokers(cfg.brokers_path)
    try:
        broker = find_broker(broker_list, query)
    except BrokerLookupError as exc:
        write("broker lookup failed: {}".format(exc))
        for candidate in exc.candidates:
            write("  - {} [{}]".format(candidate.get("name"), candidate.get("id")))
        if exc.candidates:
            write("Re-run with a more specific name or the exact broker id.")
        return 2

    identities = profiles_mod.load_scan_identities(cfg.profiles_path, cfg.profile_path)

    searx_search, page_action, closers = service_mod.build_detection(cfg)
    deps = service_mod.Dependencies(
        searx_search=searx_search, page_action=page_action, closers=closers,
    )
    try:
        return diagnose_broker(broker, identities, deps, write=write)
    finally:
        deps.close()
