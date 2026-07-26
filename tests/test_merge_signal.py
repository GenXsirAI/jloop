#!/usr/bin/env python3
"""
Unit tests for scripts/merge_signal.py (GOL-10 merge-signal finalize step).

Run:  python3 tests/test_merge_signal.py
   or: python3 -m pytest tests/test_merge_signal.py -q

Pure transforms are tested directly; the idempotency-guarded `plan` path is
exercised end-to-end with JLOOP_ACTION_DIR redirected to a temp dir, so tests
never touch the real .factory/ tree or the network.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "merge_signal.py"
PASS, FAIL = [], []


def _load():
    spec = importlib.util.spec_from_file_location("merge_signal", str(MOD))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ok(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


ms = _load()
URL = "https://github.com/GenXsirAI/jloop/pull/4"

# 1. label swap: approved -> waiting-to-merge, keep non-state, drop other states
new, added, removed = ms.swap_labels(["approved", "Improvement"])
ok("swap: waiting-to-merge added", "waiting-to-merge" in new)
ok("swap: approved removed", "approved" not in new)
ok("swap: Improvement preserved", "Improvement" in new)
ok("swap: exactly one state label", sum(l in ms.STATE_LABELS for l in new) == 1)

# 2. swap is idempotent if already waiting-to-merge
new2, added2, _ = ms.swap_labels(["waiting-to-merge", "Feature"])
ok("swap: no double-add when already waiting-to-merge", added2 == [] and new2.count("waiting-to-merge") == 1)

# 3. stray spec-waiting-approval also dropped (one-at-a-time invariant)
new3, _, rem3 = ms.swap_labels(["approved", "spec-waiting-approval", "Bug"])
ok("swap: strips stray spec-waiting-approval", "spec-waiting-approval" not in new3 and "spec-waiting-approval" in rem3)
ok("swap: still one state label", sum(l in ms.STATE_LABELS for l in new3) == 1)

# 4. callout inserted right below ## Problem, before next heading
desc = "## Problem\n\nThe thing is broken.\n\n## Acceptance Criteria\n\n- [ ] AC-1\n"
out = ms.insert_callout(desc, URL)
ok("callout: head present", ms.CALLOUT_HEAD in out)
ok("callout: url linked", f"[{URL}]({URL})" in out)
pi, ai, ci = out.index("## Problem"), out.index("## Acceptance"), out.index(ms.CALLOUT_HEAD)
ok("callout: positioned between Problem and Acceptance", pi < ci < ai)

# 5. idempotent: second insert does not stack a duplicate
out2 = ms.insert_callout(out, URL)
ok("callout: no duplicate on re-run", out2.count(ms.CALLOUT_HEAD) == 1)

# 6. idempotent refresh: url updates in place, still single block
URL2 = "https://github.com/GenXsirAI/jloop/pull/5"
out3 = ms.insert_callout(out, URL2)
ok("callout: url refreshed in place", out3.count(ms.CALLOUT_HEAD) == 1 and URL2 in out3 and URL not in out3)

# 7. fallback: no ## Problem heading -> callout prepended, still single
nohead = ms.insert_callout("some freeform body\n", URL)
ok("callout: fallback prepends when no Problem heading", nohead.count(ms.CALLOUT_HEAD) == 1 and nohead.startswith(ms.CALLOUT_HEAD))

# 8. plan is idempotency-guarded: first exit 0, second exit 3, single action file
with tempfile.TemporaryDirectory() as td:
    # Redirect BOTH the idempotency store and the action dir into the temp tree
    # (merge_signal.py honors JLOOP_ACTION_DIR), so tests never touch .factory/.
    env = dict(os.environ,
               JLOOP_ACTION_DIR=str(Path(td) / "actions"))

    def run_plan():
        return subprocess.run(
            [sys.executable, str(MOD), "plan", "GOL-10", "--url", URL,
             "--labels", json.dumps(["approved", "Improvement"]),
             "--description", desc],
            capture_output=True, text=True, env=env, cwd=td,
        )

    r1 = run_plan()
    ok("plan: first run exit 0", r1.returncode == 0)
    try:
        p1 = json.loads(r1.stdout)
        ok("plan: payload has label swap", p1["payload"]["labels_add"] == ["waiting-to-merge"])
        ok("plan: payload has callout in new_description", ms.CALLOUT_HEAD in p1["payload"]["new_description"])
    except Exception:
        ok("plan: first run JSON parseable", False)
    r2 = run_plan()
    ok("plan: second run exit 3 (idempotent)", r2.returncode == 3)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
