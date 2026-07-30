#!/usr/bin/env python3
"""Tests for the --ns namespace scoping in idempotency.py (GOL-18)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IDEM = REPO_ROOT / "scripts" / "idempotency.py"


def _run(args, action_dir):
    env = dict(os.environ)
    env["JLOOP_ACTION_DIR"] = str(action_dir)
    return subprocess.run(
        [sys.executable, str(IDEM), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
    )


def test_ns_isolation_and_backward_compat():
    d = Path(tempfile.mkdtemp())
    try:
        # default (no ns) claim + commit
        r = _run(["claim", "pr-create:GOL-18", "--meta", "{}"], d)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["ok"] is True

        # same logical key under a different namespace must NOT collide
        r2 = _run(["claim", "pr-create:GOL-18", "--ns", "teamA", "--meta", "{}"], d)
        assert r2.returncode == 0, r2.stderr
        assert json.loads(r2.stdout)["ok"] is True

        # claiming the same (ns,key) again must be refused (exit 3)
        r3 = _run(["claim", "pr-create:GOL-18", "--ns", "teamA"], d)
        assert r3.returncode == 3, r3.stdout

        # status resolves per-namespace
        s_default = _run(["status", "pr-create:GOL-18"], d)
        s_team = _run(["status", "pr-create:GOL-18", "--ns", "teamA"], d)
        assert json.loads(s_default.stdout)["exists"] is True
        assert json.loads(s_team.stdout)["exists"] is True

        # backward-compat: a key claimed without --ns persists without a prefix
        # on disk (key string has no colon prefix)
        files = list(d.glob("*.json"))
        assert len(files) == 2, [f.name for f in files]
        for f in files:
            rec = json.loads(f.read_text())
            assert rec["key"] in ("pr-create:GOL-18", "teamA:pr-create:GOL-18"), rec
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_ns_isolation_and_backward_compat()
    print("OK")
