#!/usr/bin/env python3
"""Reference impl for: bg-eraser-bridge-config

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/eraser_bridge.py'
t = p.read_text()

# 1) __init__ gains the config_path keyword (default None), stored as self.config_path.
OLD_INIT = r'''    def __init__(self, eraser_bin: str = "eraser", timeout_s: int = 300,
                 dry_run: bool = True, runner=None, cwd: str | None = None,
                 env: dict | None = None):
        self.eraser_bin = eraser_bin
        self.timeout_s = timeout_s
        self.dry_run = dry_run
        self.cwd = cwd'''
NEW_INIT = r'''    def __init__(self, eraser_bin: str = "eraser", timeout_s: int = 300,
                 dry_run: bool = True, runner=None, cwd: str | None = None,
                 env: dict | None = None, config_path: str | None = None):
        self.eraser_bin = eraser_bin
        self.timeout_s = timeout_s
        self.dry_run = dry_run
        self.cwd = cwd
        # Optional path to the eraser config file; forwarded as --config on
        # every invocation so eraser does not fall back to $HOME/.eraser/config.yaml.
        self.config_path = config_path'''

# 2) submit_removal forwards it to build_eraser_cmd.
OLD_SUBMIT = r'cmd = build_eraser_cmd(broker_id, profile, self.eraser_bin, dry_run=self.dry_run)'
NEW_SUBMIT = r'cmd = build_eraser_cmd(broker_id, profile, self.eraser_bin, dry_run=self.dry_run, config_path=self.config_path)'

# 3) status forwards it to build_eraser_status_cmd.
OLD_STATUS = r'return self._run(build_eraser_status_cmd(self.eraser_bin, limit))'
NEW_STATUS = r'return self._run(build_eraser_status_cmd(self.eraser_bin, limit, config_path=self.config_path))'

# 4) monitor forwards it to build_eraser_monitor_cmd.
OLD_MONITOR = r'result = self._run(build_eraser_monitor_cmd(self.eraser_bin, profile_id))'
NEW_MONITOR = r'result = self._run(build_eraser_monitor_cmd(self.eraser_bin, profile_id, config_path=self.config_path))'

# 5) fill forwards it to build_eraser_fill_cmd.
OLD_FILL = r'result = self._run(build_eraser_fill_cmd(self.eraser_bin, profile_id))'
NEW_FILL = r'result = self._run(build_eraser_fill_cmd(self.eraser_bin, profile_id, config_path=self.config_path))'

for old, new in [(OLD_INIT, NEW_INIT), (OLD_SUBMIT, NEW_SUBMIT),
                 (OLD_STATUS, NEW_STATUS), (OLD_MONITOR, NEW_MONITOR),
                 (OLD_FILL, NEW_FILL)]:
    assert old in t, "refimpl anchor not found -- did the target change? {!r}".format(old[:60])
    t = t.replace(old, new, 1)

p.write_text(t)
print("refimpl applied")
