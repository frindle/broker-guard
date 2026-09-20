"""Self-heal hook: turn a classified tool-side automation drift into a
dispatch-ready task descriptor for needs-fix/."""
import hashlib


def build_fix_task(failure: dict) -> dict:
    """Build a scaffoldable fix-task descriptor from a classified failure.

    Only tool-side failures are eligible; anything else raises ValueError.
    The id is the stable sha256 hex digest of failure["signature"], so the
    same drift always maps to the same task across calls.
    """
    if failure.get("class") != "tool_side":
        raise ValueError(
            "only tool_side failures are eligible for self-heal; got class={!r}"
            .format(failure.get("class")))

    target = failure["target"]
    message = failure["message"]
    return {
        "id": hashlib.sha256(failure["signature"].encode("utf-8")).hexdigest(),
        "title": "fix {}: {}".format(target, message),
        "target": target,
        "symptom": message,
        "created_at": failure["now"],
        "status": "proposed",
    }


def should_autofix(task: dict, policy: dict) -> dict:
    scaffold = bool(policy.get("auto_scaffold", False)) and task.get("status") == "proposed"
    return {"scaffold": scaffold, "requires_confirm": True}
