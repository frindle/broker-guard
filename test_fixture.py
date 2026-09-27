"""Adversarial fixture for: bg-automate-optout-form-submission

The intent defines the technique: ``run_optout_submission_pass`` is pure
orchestration, offline-testable with fakes for ``optout_submit.run_attempt``
and ``review.load_attempts``. The cases below drive the REAL function in
broker_guard/autopilot.py against those fakes and assert the exact counts,
the dedupe rule (one-shot per broker per identity), the SubmissionRefused
vs generic-exception split, and that run_forever fires the pass on its own
cadence exactly once.

Each case: (description, callable_returning_actual, expected)
"""
import pathlib
import sys
import types
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/autopilot.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


class _FakeRefused(RuntimeError):
    """Stand-in for optout_submit.SubmissionRefused (the pass catches it by
    name from the faked module, so identity with the real class is not what
    matters -- the split between this and a generic Exception is)."""


def _install_fakes(supported, records, attempt_behavior):
    """Point broker_guard.optout_forms / .review / .optout_submit at fakes.

    *attempt_behavior* maps broker_id -> "ok" | "refused" | "boom".
    Returns (calls_list, restore_fn).
    """
    import broker_guard

    # The parent package must be in sys.modules too: autopilot's lazy
    # `from broker_guard import X` resolves the submodules through it, and a
    # missing "broker_guard" entry makes Python re-import the REAL package.
    saved_parent = sys.modules.get("broker_guard")
    if saved_parent is None:
        # The parent package was never imported (the fixture loads the target
        # by file path). Register it so `from broker_guard import X` resolves
        # through our fakes instead of re-importing the REAL submodules from
        # disk.
        pkg = types.ModuleType("broker_guard")
        pkg.__path__ = [str(pathlib.Path(broker_guard.__file__).parent)]
        sys.modules["broker_guard"] = pkg

    calls = []

    def run_attempt(broker_id, identity, cfg):
        calls.append((broker_id, getattr(identity, "identity_key", None), cfg))
        behavior = attempt_behavior.get(broker_id, "ok")
        if behavior == "refused":
            raise _FakeRefused("off")
        if behavior == "boom":
            raise ValueError("secret PII must never be logged: {}".format(identity.identity_key))

    forms_fake = types.SimpleNamespace(supported_broker_ids=lambda: list(supported))
    review_fake = types.SimpleNamespace(
        review_dir=lambda cfg: "/fake/review",
        load_attempts=lambda directory, limit=None: [dict(r) for r in records],
    )
    submit_fake = types.SimpleNamespace(run_attempt=run_attempt, SubmissionRefused=_FakeRefused)

    saved = {}
    installed = []
    for name, fake in (("optout_forms", forms_fake), ("review", review_fake),
                       ("optout_submit", submit_fake)):
        mod_name = "broker_guard." + name
        saved[mod_name] = sys.modules.get(mod_name)
        saved["attr:" + name] = getattr(broker_guard, name, None)
        sys.modules[mod_name] = fake
        setattr(broker_guard, name, fake)
        installed.append(name)

    def restore():
        for name in installed:
            mod_name = "broker_guard." + name
            if saved[mod_name] is not None:
                sys.modules[mod_name] = saved[mod_name]
            else:
                sys.modules.pop(mod_name, None)
            setattr(broker_guard, name, saved["attr:" + name])
        if saved_parent is not None:
            sys.modules["broker_guard"] = saved_parent

    return calls, restore


def _identities(*keys):
    return [types.SimpleNamespace(identity_key=k) for k in keys]


CFG = types.SimpleNamespace(optout_submit_enabled=False)
BROKERS = ["alpha", "beta"]


def case_all_pairs_attempted():
    calls, restore = _install_fakes(BROKERS, [], {b: "ok" for b in BROKERS})
    try:
        result = target.run_optout_submission_pass(_identities("id1", "id2"), CFG)
        return (result, sorted((c[0], c[1]) for c in calls))
    finally:
        restore()


def case_dedupe_skips_only_matching_pair():
    records = [{"broker_id": "alpha", "identity_key": "id1"}]
    calls, restore = _install_fakes(BROKERS, records, {b: "ok" for b in BROKERS})
    try:
        result = target.run_optout_submission_pass(_identities("id1", "id2"), CFG)
        attempted_pairs = sorted((c[0], c[1]) for c in calls)
        return (result, attempted_pairs)
    finally:
        restore()


def case_refused_is_not_an_error():
    calls, restore = _install_fakes(BROKERS, [], {b: "refused" for b in BROKERS})
    try:
        result = target.run_optout_submission_pass(_identities("id1", "id2"), CFG)
        return (result, len(calls))
    finally:
        restore()


def case_generic_error_counted_and_pass_continues():
    calls, restore = _install_fakes(BROKERS, [], {"alpha": "boom", "beta": "ok"})
    try:
        result = target.run_optout_submission_pass(_identities("id1"), CFG)
        return (result, sorted(c[0] for c in calls))
    finally:
        restore()


def case_empty_identities():
    calls, restore = _install_fakes(BROKERS, [], {b: "ok" for b in BROKERS})
    try:
        result = target.run_optout_submission_pass([], CFG)
        return (result, len(calls))
    finally:
        restore()


def case_intervals_default():
    iv = target.Intervals()
    return (iv.optout_seconds, iv.confirmation_seconds)


def case_run_forever_fires_pass_once_on_cadence():
    """run_forever must call run_optout_submission_pass exactly once across
    two ticks: it fires when its counter reaches optout_seconds and then
    RESETS (a missing reset would fire it again on the next tick)."""
    import threading

    broker_guard = sys.modules["broker_guard"]
    saved_modules = {}
    for name in ("settings", "service", "profiles"):
        mod_name = "broker_guard." + name
        saved_modules[mod_name] = (sys.modules.get(mod_name), getattr(broker_guard, name, None))

    cfg = types.SimpleNamespace(interval_seconds=100, brokers_path="x",
                                profiles_path=None, profile_path=None)
    settings_fake = types.SimpleNamespace(effective_config=lambda c: c)
    service_fake = types.SimpleNamespace(write_heartbeat=lambda c, p: None)
    identity = types.SimpleNamespace(identity_key="id1")
    profiles_fake = types.SimpleNamespace(
        load_scan_identities=lambda pp, lp: [identity])

    for name, fake in (("settings", settings_fake), ("service", service_fake),
                       ("profiles", profiles_fake)):
        sys.modules["broker_guard." + name] = fake
        setattr(broker_guard, name, fake)

    saved_load_brokers = target.brokers_mod.load_brokers
    target.brokers_mod.load_brokers = lambda path: []

    store = types.SimpleNamespace(is_seen=lambda ik, b: True, seen_brokers=lambda ik: [])
    deps = target.AutopilotDependencies(store=store)

    fired = []
    real_pass = target.run_optout_submission_pass
    target.run_optout_submission_pass = lambda identities, cfg2: (fired.append((list(identities), cfg2)), {"attempted": 0, "skipped_existing": 0, "submission_disabled": 0, "errors": 0})[1]

    stop = threading.Event()
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            stop.set()

    try:
        # scan=100, confirmation=50 -> tick 50. Like the other counters,
        # elapsed_since_optout starts AT its interval (fires once on start);
        # with optout_seconds=60 it must NOT fire again after reset+tick(50).
        target.run_forever(cfg, deps, target.Intervals(scan_seconds=100,
                                                       confirmation_seconds=50,
                                                       optout_seconds=60),
                           stop, sleep=fake_sleep)
    finally:
        target.run_optout_submission_pass = real_pass
        target.brokers_mod.load_brokers = saved_load_brokers
        for name in ("settings", "service", "profiles"):
            mod_name = "broker_guard." + name
            if saved_modules[mod_name][0] is not None:
                sys.modules[mod_name] = saved_modules[mod_name][0]
            else:
                sys.modules.pop(mod_name, None)
            setattr(broker_guard, name, saved_modules[mod_name][1])

    return (len(fired), [f[1] is cfg for f in fired], sleeps)


CASES = [
    ("every broker x identity pair attempted when no review record exists",
     case_all_pairs_attempted,
     ({"attempted": 4, "skipped_existing": 0, "submission_disabled": 0, "errors": 0},
      [("alpha", "id1"), ("alpha", "id2"), ("beta", "id1"), ("beta", "id2")])),

    ("an existing review record skips ONLY its own broker+identity pair (one-shot)",
     case_dedupe_skips_only_matching_pair,
     ({"attempted": 3, "skipped_existing": 1, "submission_disabled": 0, "errors": 0},
      [("alpha", "id2"), ("beta", "id1"), ("beta", "id2")])),

    ("SubmissionRefused is the normal off-state: counted as submission_disabled, never an error",
     case_refused_is_not_an_error,
     ({"attempted": 0, "skipped_existing": 0, "submission_disabled": 4, "errors": 0}, 4)),

    ("a generic exception is counted under errors and the pass continues to the next pair",
     case_generic_error_counted_and_pass_continues,
     ({"attempted": 1, "skipped_existing": 0, "submission_disabled": 0, "errors": 1},
      ["alpha", "beta"])),

    ("empty identities: all counters zero and run_attempt never called",
     case_empty_identities,
     ({"attempted": 0, "skipped_existing": 0, "submission_disabled": 0, "errors": 0}, 0)),

    ("Intervals gains optout_seconds defaulting to the same cadence as confirmation",
     case_intervals_default,
     (21600, 21600)),

    ("run_forever fires the pass exactly once on its own cadence and resets the counter",
     case_run_forever_fires_pass_once_on_cadence,
     (1, [True], [50, 50])),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
