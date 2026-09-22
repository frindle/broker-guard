"""Live, in-process progress for the broker sweep.

Why this exists
---------------
Before this module the dashboard could only say "Scan in progress right
now." for the entire duration of a cycle -- which, across the full ~827
broker dataset with a paced SERP client, is well over an hour. There was no
way to tell a healthy slow scan from a wedged one, and no way to tell
"checked 827 brokers, genuinely found nothing" from "827 brokers all errored
out and were silently counted as clean" (see ``serpwatch.run_serpwatch``'s
per-broker error handling and ``searx_client.SearxClient``).

So a cycle now reports, per broker, one of four honest outcomes:

``hit``      the broker's own domain carried a matching listing
``checked``  searched successfully, nothing matched
``error``    the check could not be completed (timeout, rate limit, 5xx...)
``skipped``  nothing to check (no usable URL, not an automatable site)

``checked + hit + error + skipped == processed`` always, and ``error`` is
kept strictly distinct from ``checked`` so "0 hits" can never again be
mistaken for "0 problems".

Scope: ONE process
------------------
This is a module-level singleton, not a file or a database. That is
sufficient and correct for the deployed topology: ``webapp.run_web_server``
runs the autopilot loop as a background *thread* inside the same
interpreter that serves the FastAPI routes, and the ``/scan`` route's job
also runs as a thread in that process (see webapp.py's module docstring on
why there is deliberately only ever one process). A second process would
see an empty snapshot rather than wrong data -- ``active`` is False until
something in THIS process starts a phase, so the dashboard falls back to
the heartbeat file, exactly as it did before.

Aggregate counters AND a per-broker map
---------------------------------------
The counters above answer "how far along is this scan"; they cannot answer
"which brokers am I clean on". That second question had no answer anywhere
in the process -- the observer closure literally threw ``broker_id`` away --
so ``/brokers``, which only ever lists brokers with a ``presence`` row (i.e.
brokers the person was FOUND on), stayed empty for a whole clean scan. A
run that checked 827 brokers and found nothing looked exactly like a run
that had not started.

So a cycle now also keeps ``brokers``: ``broker_id -> {outcome, hits,
errors, checked_at, phase, identity_key}``. A broker absent from that map
has NOT been reached yet in this cycle; it is deliberately never
represented as ``checked``, which is the same "unknown is not absent"
principle the error/checked split exists for.

Cycle, not phase
----------------
``start()`` resets the AGGREGATE counters per phase, because the SERP leg
and the browser leg have different denominators (the browser leg only runs
over the automatable subset -- see ``playwright_checks.build_site_checks``)
and "412/827" must mean the phase actually running. The per-broker map
must NOT follow that rule: the two legs run sequentially inside ONE cycle
(``service.build_presence_checker``), so resetting per phase would throw
away every SERP result the moment the browser leg started and leave the
finished cycle reporting only the handful of browser-checked brokers.
``begin_cycle()`` is therefore the per-broker map's reset point, called
once per cycle before either leg. When both legs report on the same
broker their outcomes are merged by ``OUTCOME_RANK`` -- hit > error >
checked > skipped -- which is exactly the precedence
``service.build_presence_checker``'s ``presence_checker`` already applies
(a hit from either leg is a hit; an errored browser check is "unknown"
even when the SERP leg came back clean).

Identity
--------
Each entry carries the ``identity_key`` of the profile that was active
when it was recorded (``profile.Identity.identity_key`` -- the SAME key
``state.py`` scopes ``presence``/``broker_status`` by; this module does not
derive a second one). Only the active profile is ever scanned, so a cycle's
entries all carry one key, but tagging them means a page filtered to
profile B cannot silently show profile A's results.

Every mutation takes ``self._lock``; ``snapshot()`` returns a plain dict
copy taken under that same lock, so a reader can never observe a half
updated set of counters.
"""
import threading
from datetime import datetime, timezone

PHASE_SERP = "serp"
PHASE_BROWSER = "browser"

#: The per-broker outcomes ``record()`` accepts. Anything else raises --
#: a typo'd outcome silently inflating the wrong bucket is exactly the
#: class of bug this module was written to eliminate.
OUTCOMES = ("hit", "checked", "error", "skipped")

#: How two legs' outcomes for the SAME broker are merged inside one cycle:
#: the higher rank wins. Mirrors ``service.build_presence_checker``'s
#: ``presence_checker`` -- a hit from either leg is a hit, and a failed
#: check outranks a clean one because "unknown" must never be displayed as
#: "nothing there".
OUTCOME_RANK = {"skipped": 1, "checked": 2, "error": 3, "hit": 4}

#: The outcome reported for a broker the current cycle has not reached.
#: Never one of OUTCOMES -- record() must not be able to produce it, and a
#: renderer must not be able to confuse it with ``checked``.
OUTCOME_PENDING = "pending"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScanProgress:
    """Thread-safe counters for the broker sweep currently in flight.

    ``now`` is injectable so tests can assert timestamps without freezing
    the real clock -- the same seam ``service.Dependencies.now`` and
    ``autopilot.AutopilotDependencies.now`` already use.
    """

    def __init__(self, now=None):
        self._lock = threading.Lock()
        self._now = now or utcnow_iso
        self._clear_locked()

    # -- mutation -----------------------------------------------------

    def _clear_counters_locked(self) -> None:
        """Reset only the AGGREGATE, per-phase counters."""
        self.active = False
        self.phase = None
        self.total = 0
        self.processed = 0
        self.hits = 0
        self.checked = 0
        self.errors = 0
        self.skipped = 0
        self.started_at = None
        self.updated_at = None

    def _clear_cycle_locked(self) -> None:
        """Reset only the per-CYCLE, per-broker state."""
        self.brokers = {}
        self.identity_key = None
        self.cycle_total = 0
        self.cycle_started_at = None

    def _clear_locked(self) -> None:
        self._clear_counters_locked()
        self._clear_cycle_locked()

    def clear(self) -> None:
        """Reset to the "nothing has run in this process" state."""
        with self._lock:
            self._clear_locked()

    def start(self, phase: str, total: int) -> None:
        """Begin a phase over *total* brokers, resetting the counters.

        Starting a new phase deliberately zeroes the counters rather than
        accumulating across phases: the dashboard shows "412/827" for the
        phase actually running, and a SERP phase followed by a browser
        phase over a different-sized subset would otherwise produce a
        nonsense denominator.

        Known limit: two sweeps running at once (the autopilot's scheduled
        cycle and a manual /scan click) share this one counter, so the
        second to start resets it and the display follows whichever is
        newer. The dashboard makes that hard to reach -- the button is
        disabled for as long as ANY scan is running -- and the failure mode
        is a briefly confusing number, never a wrong scan result, so a
        per-sweep registry is not worth the complexity here.

        The per-broker map is NOT reset here when a cycle is open (see
        ``begin_cycle``): the SERP and browser legs are two phases of one
        cycle, and their per-broker results have to coexist. A ``start()``
        with no cycle open -- a caller that drives a phase directly, as
        several tests do -- does clear it, so the map can never accumulate
        across unrelated sweeps.
        """
        with self._lock:
            self._clear_counters_locked()
            if self.cycle_started_at is None:
                self._clear_cycle_locked()
            self.active = True
            self.phase = phase
            self.total = max(0, int(total))
            self.started_at = self._now()
            self.updated_at = self.started_at

    def record(self, outcome: str, count: int = 1) -> None:
        """Count *count* brokers as having finished with *outcome*.

        Never raises for a phase that was never started -- a stray record
        just increments the counters with ``total`` still 0, which
        ``snapshot()`` reports as an unknown-denominator progress rather
        than dividing by zero.
        """
        _check_outcome(outcome)
        if count <= 0:
            return
        with self._lock:
            self._record_locked(outcome, count)

    def _record_locked(self, outcome: str, count: int = 1) -> None:
        self.processed += count
        setattr(self, _FIELD_FOR[outcome], getattr(self, _FIELD_FOR[outcome]) + count)
        self.updated_at = self._now()

    def begin_cycle(self, identity_key: str | None = None, total: int = 0) -> None:
        """Open a new cycle: clear the per-broker map, tag it with the
        identity being scanned, and record how many brokers it covers.

        Called once per cycle (``service.build_presence_checker``) BEFORE
        either leg starts, which is what lets the SERP and browser legs'
        per-broker results coexist while ``start()`` goes on resetting the
        aggregate counters per phase. Also clears the aggregate counters,
        so a cycle that reaches ``begin_cycle`` and then does nothing (no
        search backend, no browser) does not leave the previous cycle's
        numbers on screen as if they were this one's.
        """
        with self._lock:
            self._clear_counters_locked()
            self._clear_cycle_locked()
            self.identity_key = identity_key
            self.cycle_total = max(0, int(total))
            self.cycle_started_at = self._now()

    def record_outcome(self, broker_id, outcome: str, hits: int = 0, errors: int = 0) -> None:
        """Record ONE broker's outcome: the aggregate counters AND the
        per-broker map, under a single acquisition of the lock.

        ``record()``'s behaviour is unchanged and still usable on its own;
        this is the strictly larger operation, not a replacement. A
        ``broker_id`` of ``None`` still counts toward the aggregate (a
        caller with nothing to key by must not silently lose the tick).
        """
        _check_outcome(outcome)
        with self._lock:
            self._record_locked(outcome, 1)
            if broker_id is None:
                return
            key = str(broker_id)
            entry = self.brokers.get(key)
            if entry is None:
                entry = {"broker_id": key, "outcome": outcome, "hits": 0,
                         "errors": 0, "checked_at": None, "phase": None,
                         "identity_key": self.identity_key}
                self.brokers[key] = entry
            elif OUTCOME_RANK[outcome] >= OUTCOME_RANK[entry["outcome"]]:
                entry["outcome"] = outcome
            entry["hits"] += max(0, int(hits or 0))
            entry["errors"] += max(0, int(errors or 0))
            entry["checked_at"] = self.updated_at
            entry["phase"] = self.phase
            entry["identity_key"] = self.identity_key

    def finish(self) -> None:
        """Mark the phase complete, KEEPING the counters.

        The last cycle's totals stay readable after it ends (that is what
        lets the dashboard say "last scan: checked 827, 3 errors" rather
        than dropping straight back to a bare timestamp); only ``active``
        flips off.
        """
        with self._lock:
            self.active = False
            self.updated_at = self._now()

    # -- reading ------------------------------------------------------

    def snapshot(self, include_brokers: bool = False) -> dict:
        """A consistent plain-dict copy of every counter.

        ``percent`` is None -- not 0 -- when ``total`` is 0, so a caller
        cannot render a confident "0%" for a denominator nobody knows.

        ``include_brokers`` adds the per-broker map (a deep-enough copy:
        fresh dicts, so a reader cannot mutate live state). It is off by
        default because the dashboard's 1.5s poll wants five integers, not
        827 objects; only ``/brokers`` asks for the map.

        ``recorded``/``not_reached`` are always present: ``not_reached``
        is how many of this cycle's brokers have not been reached YET, and
        is deliberately not folded into any outcome bucket.
        """
        with self._lock:
            total = self.total
            processed = self.processed
            percent = None
            if total > 0:
                percent = int(min(100, round(processed * 100.0 / total)))
            recorded = len(self.brokers)
            snap = {
                "active": self.active,
                "phase": self.phase,
                "total": total,
                "processed": processed,
                "hits": self.hits,
                "checked": self.checked,
                "errors": self.errors,
                "skipped": self.skipped,
                "percent": percent,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "identity_key": self.identity_key,
                "cycle_total": self.cycle_total,
                "cycle_started_at": self.cycle_started_at,
                "recorded": recorded,
                "not_reached": max(0, self.cycle_total - recorded),
            }
            if include_brokers:
                snap["brokers"] = {bid: dict(entry) for bid, entry in self.brokers.items()}
            return snap

    def observer(self):
        """A ``(broker_id, outcome, hits, errors) -> None`` callable.

        This is the shape ``serpwatch.run_serpwatch`` and
        ``playwright_checks.run_playwright_checks`` accept as their
        ``observer`` argument. Returning a closure rather than making
        ScanProgress itself callable keeps those modules depending on a
        plain function, so they stay testable with a list-appending lambda
        and never import this module.
        """

        def _observe(broker_id, outcome, hits=0, errors=0):
            self.record_outcome(broker_id, outcome, hits, errors)

        return _observe


def _check_outcome(outcome: str) -> None:
    if outcome not in OUTCOMES:
        raise ValueError(
            "unknown progress outcome {!r} (expected one of {})".format(
                outcome, ", ".join(OUTCOMES)
            )
        )


_FIELD_FOR = {
    "hit": "hits",
    "checked": "checked",
    "error": "errors",
    "skipped": "skipped",
}

# The process-wide instance the autopilot loop writes and the web routes
# read. Exposed through current() rather than imported directly so a test
# can swap it for an isolated instance without reaching into module
# globals from the outside.
_CURRENT = ScanProgress()


def current() -> ScanProgress:
    return _CURRENT


def snapshot(include_brokers: bool = False) -> dict:
    return _CURRENT.snapshot(include_brokers=include_brokers)
