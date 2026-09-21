"""Stub for broker_guard/scheduler.py -- implement per TASK.md."""

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
    dt = datetime.fromisoformat(last_run_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=interval_seconds)).astimezone(timezone.utc).isoformat()
