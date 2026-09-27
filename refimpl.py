#!/usr/bin/env python3
"""Reference impl for: bg-automate-optout-form-submission

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/autopilot.py'
t = p.read_text()


def _replace(old, new):
    global t
    assert old in t, "refimpl anchor not found -- did the target change?\n" + old[:200]
    assert t.count(old) == 1, "refimpl anchor is ambiguous: " + old[:80]
    t = t.replace(old, new, 1)


# --- 1. New pure function, right after run_confirmation_pass -----------------
OLD_1 = r'''    log.info("autopilot confirmation pass complete", extra={
        "monitor_ran": monitor_result is not None,
        "status_ran": status_result is not None,
    })
    return {"monitor": monitor_result, "status": status_result}


def build_dependencies(cfg: Config) -> AutopilotDependencies:'''.rstrip()

NEW_1 = r'''    log.info("autopilot confirmation pass complete", extra={
        "monitor_ran": monitor_result is not None,
        "status_ran": status_result is not None,
    })
    return {"monitor": monitor_result, "status": status_result}


def run_optout_submission_pass(identities: list, cfg) -> dict:
    """One automated opt-out FORM submission pass for every saved profile.

    For each identity and each broker in ``optout_forms.supported_broker_ids()``
    this either SKIPS the pair (a review record already exists for that
    broker + identity -- any outcome; submission is one-shot per broker per
    identity, never resubmitted) or calls the ALREADY-EXISTING
    ``optout_submit.run_attempt(broker_id, identity, cfg)``, which owns all
    real browser interaction. This function itself opens no page and touches
    no browser internals -- it only orchestrates and dedupes against the
    review folder (which is already the audit trail).

    Never raises out of a pair: ``optout_submit.SubmissionRefused`` is the
    NORMAL state when ``cfg.optout_submit_enabled`` is False, so it is logged
    at debug level and counted as ``submission_disabled``, not an error. Any
    other exception is logged as a warning carrying ONLY the broker_id and
    the exception TYPE name -- never the message or any field value (the
    review.py PII-logging discipline) -- and counted under ``errors``; the
    pass continues to the next pair either way.

    Returns ``{'attempted': n, 'skipped_existing': n,
    'submission_disabled': n, 'errors': n}``.
    """
    from broker_guard import optout_forms as optout_forms_mod
    from broker_guard import optout_submit as optout_submit_mod
    from broker_guard import review as review_mod

    counts = {"attempted": 0, "skipped_existing": 0,
              "submission_disabled": 0, "errors": 0}
    if not identities:
        return counts

    directory = review_mod.review_dir(cfg)
    try:
        existing = review_mod.load_attempts(directory)
    except Exception as exc:
        log.warning("could not read the review folder; treating it as empty",
                    extra={"error": type(exc).__name__})
        existing = []

    for identity in identities:
        identity_key = getattr(identity, "identity_key", "") or ""
        for broker_id in optout_forms_mod.supported_broker_ids():
            if any(r.get("broker_id") == broker_id and r.get("identity_key") == identity_key
                   for r in existing):
                counts["skipped_existing"] += 1
                continue
            try:
                optout_submit_mod.run_attempt(broker_id, identity, cfg)
                counts["attempted"] += 1
            except optout_submit_mod.SubmissionRefused:
                # The expected state while the feature is off -- not an error.
                log.debug("opt-out submission refused for broker %s", broker_id)
                counts["submission_disabled"] += 1
            except Exception as exc:
                # Type name only, never the message or any field value.
                log.warning("opt-out submission failed for broker %s (%s)",
                            broker_id, type(exc).__name__)
                counts["errors"] += 1

    log.info("autopilot opt-out submission pass complete", extra=dict(counts))
    return counts


def build_dependencies(cfg: Config) -> AutopilotDependencies:'''.rstrip()

_replace(OLD_1, NEW_1)

# --- 2. Intervals gains the third knob ---------------------------------------
OLD_2 = r'''@dataclass
class Intervals:
    scan_seconds: int = 86400
    confirmation_seconds: int = 21600  # check for replies more often than a full re-scan'''.rstrip()

NEW_2 = r'''@dataclass
class Intervals:
    scan_seconds: int = 86400
    confirmation_seconds: int = 21600  # check for replies more often than a full re-scan
    optout_seconds: int = 21600  # automated opt-out form submission, same cadence as confirmation'''.rstrip()

_replace(OLD_2, NEW_2)

# --- 3. Initialize the third counter ------------------------------------------
OLD_3 = r'''    scan_seconds = _live_scan_seconds()
    tick_seconds = max(1, min(scan_seconds, intervals.confirmation_seconds))
    elapsed_since_scan = scan_seconds
    elapsed_since_confirmation = intervals.confirmation_seconds'''.rstrip()

NEW_3 = r'''    scan_seconds = _live_scan_seconds()
    tick_seconds = max(1, min(scan_seconds, intervals.confirmation_seconds))
    elapsed_since_scan = scan_seconds
    elapsed_since_confirmation = intervals.confirmation_seconds
    elapsed_since_optout = intervals.optout_seconds'''.rstrip()

_replace(OLD_3, NEW_3)

# --- 4. The pass itself, after the confirmation block --------------------------
OLD_4 = r'''        if elapsed_since_confirmation >= intervals.confirmation_seconds:
            try:
                run_confirmation_pass(deps)
            except Exception as exc:
                log.exception("autopilot confirmation pass failed",
                               extra={"error": "{}: {}".format(type(exc).__name__, exc)})
            elapsed_since_confirmation = 0'''.rstrip()

NEW_4 = r'''        if elapsed_since_confirmation >= intervals.confirmation_seconds:
            try:
                run_confirmation_pass(deps)
            except Exception as exc:
                log.exception("autopilot confirmation pass failed",
                               extra={"error": "{}: {}".format(type(exc).__name__, exc)})
            elapsed_since_confirmation = 0

        if elapsed_since_optout >= intervals.optout_seconds:
            try:
                run_optout_submission_pass(_scan_identities(), cfg)
            except Exception as exc:
                log.exception("autopilot opt-out submission pass failed",
                               extra={"error": "{}: {}".format(type(exc).__name__, exc)})
            elapsed_since_optout = 0'''.rstrip()

_replace(OLD_4, NEW_4)

# --- 5. Tick the third counter --------------------------------------------------
OLD_5 = r'''        sleep(tick_seconds)
        elapsed_since_scan += tick_seconds
        elapsed_since_confirmation += tick_seconds'''.rstrip()

NEW_5 = r'''        sleep(tick_seconds)
        elapsed_since_scan += tick_seconds
        elapsed_since_confirmation += tick_seconds
        elapsed_since_optout += tick_seconds'''.rstrip()

_replace(OLD_5, NEW_5)

p.write_text(t)
print("refimpl applied")
