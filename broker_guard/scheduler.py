"""Scheduling helpers: launchd plists, cron lines and next-run arithmetic.

These build the *descriptors* for an external scheduler. The container image
does not use them -- ``broker_guard.service`` runs its own interval loop -- but
they remain the supported way to run broker-guard from launchd or cron on a
host, and ``next_run_time`` is used by the service loop to log its next wake.
"""

from datetime import datetime, timedelta, timezone


def build_launchd_plist(label: str, program_args: list[str], interval_seconds: int) -> dict:
    return {
        "Label": label,
        "ProgramArguments": program_args,
        "StartInterval": interval_seconds,
    }


def build_cron_line(schedule_expr: str, command: str) -> str:
    if len(schedule_expr.split()) != 5:
        raise ValueError("schedule_expr must have exactly 5 fields")
    return "{} {}".format(schedule_expr, command)


def next_run_time(last_run_iso: str, interval_seconds: int) -> str:
    if not isinstance(last_run_iso, str) or not last_run_iso.strip():
        raise ValueError("last_run_iso must be a non-empty ISO-8601 string")
    if not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool):
        raise ValueError("interval_seconds must be an int")
    dt = datetime.fromisoformat(last_run_iso.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=interval_seconds)).astimezone(timezone.utc).isoformat()
