#!/usr/bin/env python3
"""
Unit tests for scripts/merge_detect.py (GOL-12 merged-PR detection step).

Run:  python3 tests/test_merge_detect.py
   or: python3 -m pytest tests/test_merge_detect.py -q

Pure transforms are tested directly; the idempotency-guarded `plan` path is
exercised end-to-end with JLOOP_ACTION_DIR redirected to a temp dir, so tests
never touch the real .factory/ tree or the network.

Aligned with the 6-label state machine (merge_signal.py AC-1):
    spec-waiting-approval -> approved -> build-in-progress -> build-complete
    -> waiting-to-merge -> done
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "merge_detect.py"
PASS, FAIL = [], []


def _load():
    spec = importlib.util.spec_from_file_location("merge_detect", str(MOD))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ok(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


md = _load()
URL = "https://github.com/GenXsirAI/jloop/pull/9"

# 1. flip: ready callout head becomes merged head, link line preserved
desc = ("## Problem\n\nThe thing is broken.\n\n"
        f"**✅ Solution Ready For Merge**\n[{URL}]({URL})\n\n"
        "## Acceptance Criteria\n\n- [ ] AC-1\n")
out = md.flip_callout(desc)
ok("flip: ready head replaced", md.MERGED_HEAD in out and md.READY_HEAD not in out)
ok("flip: link line preserved", f"[{URL}]({URL})" in out)
ok("flip: positioned between Problem and Acceptance",
   out.index("## Problem") < out.index(md.MERGED_HEAD) < out.index("## Acceptance"))

# 2. idempotent: already-flipped callout is untouched (no double flip)
out2 = md.flip_callout(out)
ok("flip: no double flip on merged text", out2 == out)

# 3. no-op when no callout present at all
plain = "## Problem\n\nnothing here.\n"
ok("flip: no callout -> unchanged", md.flip_callout(plain) == plain)

# 4. label transition (default): waiting-to-merge removed, done added,
#    build-complete PRESERVED (6-label machine record of shipped impl)
new, added, removed = md.transition_labels(["build-complete", "waiting-to-merge", "Improvement"])
ok("transition: waiting-to-merge removed", "waiting-to-merge" not in new)
ok("transition: done added", "done" in new)
ok("transition: build-complete preserved", "build-complete" in new)
ok("transition: Improvement preserved", "Improvement" in new)
ok("transition: exactly the build-complete + done pair (or + non-state)",
   sum(l in md.STATE_LABELS for l in new) == 2)

# 5. label transition: --no-set-done leaves done unset
newc, addedc, removedc = md.transition_labels(
    ["build-complete", "waiting-to-merge", "Improvement"], set_done=False)
ok("transition: no-set-done drops waiting-to-merge", "waiting-to-merge" not in newc)
ok("transition: no-set-done does NOT add done", "done" not in newc)
ok("transition: no-set-done preserves build-complete", "build-complete" in newc)

# 6. label transition: stray build-in-progress transient is dropped
newp, _, remp = md.transition_labels(
    ["approved", "build-in-progress", "waiting-to-merge", "Bug"])
ok("transition: stray build-in-progress dropped", "build-in-progress" not in newp)
ok("transition: approved dropped (human gate, now stale)", "approved" not in newp)
ok("transition: done terminal state reached", "done" in newp)

# 7. plan is idempotency-guarded: first exit 0, second exit 3
with tempfile.TemporaryDirectory() as td:
    env = dict(os.environ, JLOOP_ACTION_DIR=str(Path(td) / "actions"))

    def run_plan():
        return subprocess.run(
            [sys.executable, str(MOD), "plan", "GOL-12", "--url", URL,
             "--labels", json.dumps(["build-complete", "waiting-to-merge", "Improvement"]),
             "--description", desc],
            capture_output=True, text=True, env=env, cwd=td,
        )

    r1 = run_plan()
    ok("plan: first run exit 0", r1.returncode == 0)
    try:
        p1 = json.loads(r1.stdout)
        ok("plan: payload flips callout head", md.MERGED_HEAD in p1["payload"]["new_description"])
        ok("plan: payload drops waiting-to-merge", "waiting-to-merge" in p1["payload"]["labels_remove"])
        ok("plan: payload adds done", "done" in p1["payload"]["labels_add"])
        ok("plan: payload preserves build-complete", "build-complete" in p1["payload"]["labels_final"])
    except Exception:
        ok("plan: first run JSON parseable", False)
    r2 = run_plan()
    ok("plan: second run exit 3 (idempotent)", r2.returncode == 3)

# 8. plan no-op (exit 3) when already flipped and labels already final
with tempfile.TemporaryDirectory() as td:
    env = dict(os.environ, JLOOP_ACTION_DIR=str(Path(td) / "actions"))
    r = subprocess.run(
        [sys.executable, str(MOD), "plan", "GOL-12B", "--url", URL,
         "--labels", json.dumps(["build-complete", "done", "Improvement"]),
         "--description", out],  # `out` already has the merged head, no waiting-to-merge
        capture_output=True, text=True, env=env, cwd=td,
    )
    ok("plan: already-merged state is a no-op (exit 3)", r.returncode == 3)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
