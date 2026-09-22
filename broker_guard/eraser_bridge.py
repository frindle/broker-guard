"""Real subprocess invocation of the vendored ``eraser`` removal engine.

Safety properties, all load-bearing:

* ``shell=False`` always, argv as a list -- there is no shell, so no quoting or
  metacharacter injection path exists even for a hostile broker dataset.
* the broker id is validated against a strict allowlist regex in
  ``eraser.build_eraser_cmd`` before it reaches argv.
* no PII on the command line: eraser reads the identity from its own config,
  so the person's name never appears in the process table.
* a timeout is always set; a wedged eraser cannot hang the service loop.
* dry-run is the DEFAULT. Sending real opt-out requests is opt-in via
  ``BG_ERASER_DRY_RUN=false``.
"""
import logging
import os
import shutil
import subprocess

from broker_guard.eraser import (
    build_eraser_cmd,
    build_eraser_fill_cmd,
    build_eraser_monitor_cmd,
    build_eraser_status_cmd,
    parse_eraser_result,
)

log = logging.getLogger("broker_guard.eraser")


class EraserUnavailable(RuntimeError):
    """The eraser binary could not be found or executed."""


class EraserBridge:
    """Invoke eraser for a broker and report structured success/failure."""

    def __init__(self, eraser_bin: str = "eraser", timeout_s: int = 300,
                 dry_run: bool = True, runner=None, cwd: str | None = None,
                 env: dict | None = None):
        self.eraser_bin = eraser_bin
        self.timeout_s = timeout_s
        self.dry_run = dry_run
        self.cwd = cwd
        # `runner` is the injection seam: tests pass a fake, production gets
        # subprocess.run.
        self._runner = runner or subprocess.run
        self._env = env

    def available(self) -> bool:
        """True when the configured binary exists and looks executable."""
        if os.path.sep in self.eraser_bin:
            return os.path.isfile(self.eraser_bin) and os.access(self.eraser_bin, os.X_OK)
        return shutil.which(self.eraser_bin) is not None

    def _run(self, cmd: list[str]) -> dict:
        try:
            completed = self._runner(
                cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                cwd=self.cwd,
                env=self._env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise EraserUnavailable(f"eraser binary not found: {self.eraser_bin}") from exc
        except PermissionError as exc:
            raise EraserUnavailable(f"eraser binary not executable: {self.eraser_bin}") from exc
        except subprocess.TimeoutExpired:
            return {"success": False, "detail": f"eraser timed out after {self.timeout_s}s",
                    "timed_out": True}
        except OSError as exc:
            raise EraserUnavailable(f"could not launch eraser: {exc}") from exc

        stdout = getattr(completed, "stdout", "") or ""
        stderr = getattr(completed, "stderr", "") or ""
        result = parse_eraser_result(stdout, getattr(completed, "returncode", 1))
        if stderr and not result["success"]:
            result["detail"] = (result["detail"] + "\n" + stderr.strip())[:2000]
        result["timed_out"] = False
        return result

    def submit_removal(self, broker_id: str, profile: dict) -> dict:
        """Ask eraser to send the opt-out for *broker_id*.

        Returns ``{'success', 'detail', 'timed_out', 'dry_run', 'broker_id'}``.
        Never raises for a broker-level failure -- the caller records it and
        moves on -- but does raise EraserUnavailable if the binary itself is
        missing, which is a config problem rather than a broker problem.
        """
        cmd = build_eraser_cmd(broker_id, profile, self.eraser_bin, dry_run=self.dry_run)
        log.info("eraser invoke", extra={"broker_id": broker_id, "dry_run": self.dry_run,
                                         "argv_len": len(cmd)})
        result = self._run(cmd)
        result["broker_id"] = broker_id
        result["dry_run"] = self.dry_run
        log.info("eraser result", extra={"broker_id": broker_id,
                                         "success": result["success"],
                                         "dry_run": self.dry_run})
        return result

    def status(self, limit: int = 50) -> dict:
        """Run ``eraser status`` for re-verification / reporting."""
        return self._run(build_eraser_status_cmd(self.eraser_bin, limit))

    def monitor(self, profile_id: str | None = None) -> dict:
        """Run ``eraser monitor`` -- one IMAP inbox pass for broker replies.

        Whole-account, not per-broker (see ``eraser.build_eraser_monitor_cmd``).
        Never raises for a scan-level failure (a missing/misconfigured inbox
        is reported in ``result['detail']``, not an exception) -- only a
        missing/unexecutable eraser binary raises EraserUnavailable, same as
        every other method here.
        """
        log.info("eraser monitor invoke", extra={"profile_id": profile_id})
        result = self._run(build_eraser_monitor_cmd(self.eraser_bin, profile_id))
        log.info("eraser monitor result", extra={"success": result["success"]})
        return result

    def fill(self, profile_id: str | None = None) -> dict:
        """Run ``eraser fill`` -- one whole-pipeline browser-automation pass.

        Whole-account, not per-broker, and not currently invoked by
        ``broker_guard.autopilot`` for that reason: there is no documented
        way to scope this to a single broker or attach a specific ID-document
        image (see ``eraser.build_eraser_fill_cmd``). Exposed here as a thin,
        correct wrapper so a future per-broker-capable eraser CLI (or a
        maintenance-window caller) has something real to call rather than a
        stub.
        """
        log.info("eraser fill invoke", extra={"profile_id": profile_id})
        result = self._run(build_eraser_fill_cmd(self.eraser_bin, profile_id))
        log.info("eraser fill result", extra={"success": result["success"]})
        return result


def noop_bridge(broker_id: str, profile: dict) -> dict:
    """Stand-in used when BG_ERASER_ENABLED is false (the default)."""
    return {"success": False, "detail": "eraser disabled (BG_ERASER_ENABLED=false)",
            "timed_out": False, "broker_id": broker_id, "dry_run": True, "skipped": True}
