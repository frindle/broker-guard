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

    def _clear_locked(self) -> None:
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
        """
        with self._lock:
            self._clear_locked()
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
        if outcome not in OUTCOMES:
            raise ValueError(
                "unknown progress outcome {!r} (expected one of {})".format(
                    outcome, ", ".join(OUTCOMES)
                )
            )
        if count <= 0:
            return
        with self._lock:
            self.processed += count
            setattr(self, _FIELD_FOR[outcome], getattr(self, _FIELD_FOR[outcome]) + count)
            self.updated_at = self._now()

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

    def snapshot(self) -> dict:
        """A consistent plain-dict copy of every counter.

        ``percent`` is None -- not 0 -- when ``total`` is 0, so a caller
        cannot render a confident "0%" for a denominator nobody knows.
        """
        with self._lock:
            total = self.total
            processed = self.processed
            percent = None
            if total > 0:
                percent = int(min(100, round(processed * 100.0 / total)))
            return {
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
            }

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
            self.record(outcome)

        return _observe


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


def snapshot() -> dict:
    return _CURRENT.snapshot()
