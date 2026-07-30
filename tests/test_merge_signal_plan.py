#!/usr/bin/env python3
"""GOL-21: merge_signal.py plan fail-closed guard.

The plan payload must carry labels_add, labels_remove, and new_description.
If any is missing the call must exit non-zero (fail-closed), never emit a
partial payload. We exercise this by unit-testing the emit path directly.
"""
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "scripts" / "merge_signal.py"


def load():
    spec = importlib.util.spec_from_file_location("merge_signal_test", SPEC)
    assert spec is not None, f"cannot load spec for {SPEC}"
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestFailClosed(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        # Isolate the durable action dir + idempotency so we don't touch .factory
        self.td = Path(__file__).resolve().parent / "_tmp_ms_actions"
        self.td.mkdir(parents=True, exist_ok=True)
        self._patchers = [
            mock.patch.object(self.mod, "ACTION_DIR", self.td),
            mock.patch.object(self.mod, "IDEMPOTENCY", REPO / "scripts" / "_no_idem_here.py"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        for f in self.td.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass

    def _run_plan(self, issue, url, labels, description):
        descs = []
        errs = []

        def fake_plan(issue, url, labels, description):
            # Replicate cmd_plan's emit so we can assert the real function's
            # exit code without the idempotency side effects.
            return self.mod.cmd_plan(issue, url, labels, description)

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            rc = fake_plan(issue, url, labels, description)
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_normal_emits_all_keys(self):
        rc, out, err = self._run_plan(
            "GOL-TEST", "https://github.com/x/y/pull/1", ["approved"],
            "## Problem\nbody")
        self.assertEqual(rc, 0, f"expected success, stderr={err}")
        payload = json.loads(out)["payload"]
        self.assertIn("labels_add", payload)
        self.assertIn("labels_remove", payload)
        self.assertIn("new_description", payload)

    def test_missing_new_description_fails_closed(self):
        # Force new_description to be dropped by stubbing insert_callout.
        with mock.patch.object(self.mod, "insert_callout", return_value=None):
            rc, out, err = self._run_plan(
                "GOL-TEST", "https://github.com/x/y/pull/1", ["approved"],
                "## Problem\nbody")
        self.assertNotEqual(rc, 0, "missing new_description must fail-closed")
        self.assertIn("missing required", err)

    def test_missing_labels_add_fails_closed(self):
        with mock.patch.object(self.mod, "swap_labels",
                                return_value=([], None, [])):  # added=None
            rc, out, err = self._run_plan(
                "GOL-TEST", "https://github.com/x/y/pull/1", ["approved"],
                "## Problem\nbody")
        self.assertNotEqual(rc, 0, "missing labels_add must fail-closed")
        self.assertIn("missing required", err)

    def test_verify_matches(self):
        desc = self.mod.insert_callout("## Problem\nbody",
                                       "https://github.com/x/y/pull/1")
        rc = self.mod.cmd_verify("GOL-TEST", "https://github.com/x/y/pull/1", desc)
        self.assertEqual(rc, 0, "verify passes when callout url == pr url")

    def test_verify_mismatch_fails(self):
        # callout points at the abandoned PR #17, finalize was for #20
        desc = self.mod.insert_callout("## Problem\nbody",
                                       "https://github.com/x/y/pull/17")
        rc = self.mod.cmd_verify("GOL-TEST", "https://github.com/x/y/pull/20", desc)
        self.assertNotEqual(rc, 0, "verify fails on stale/abandoned PR url")

    def test_verify_trailing_slash_tolerated(self):
        # a callout built with/without a trailing slash must still match
        desc = self.mod.insert_callout("## Problem\nbody",
                                       "https://github.com/x/y/pull/1")
        rc = self.mod.cmd_verify("GOL-TEST", "https://github.com/x/y/pull/1/", desc)
        self.assertEqual(rc, 0, "trailing slash should not break the match")


if __name__ == "__main__":
    unittest.main()
