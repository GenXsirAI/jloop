#!/usr/bin/env python3
"""
jloop merge-detect — the "PR actually merged" step (GOL-12).

GOL-10 (merge_signal.py) inserts a  **✅ Solution Ready For Merge**  callout
when the PR is opened, and transitions the issue to the
`build-complete + waiting-to-merge` state (6-label machine:
spec-waiting-approval -> approved -> build-in-progress -> build-complete ->
waiting-to-merge -> done). But nothing reflected the moment the human
*actually merges* that PR back onto the issue. This script closes that gap:
once the PR is confirmed merged, it rewrites the callout to
**✅ Merged / Complete** and clears the now-stale `waiting-to-merge` signal,
advancing to `done` while preserving `build-complete` (a record that the
PR's implementation is done).

Two side effects, both idempotency-guarded (keyed `merge-detect:<issue>`):
  1. CALLOUT FLIP (the requested change): replace the `Solution Ready For
     Merge` callout head with `Merged / Complete`, preserving its PR link
     line. If the callout is already flipped, or absent, the run is a no-op.
  2. LABEL TRANSITION: remove `waiting-to-merge` (the build-set signal is now
     stale) and add `done`. `build-complete` is preserved by default — per the
     6-label machine it persists through the final phase as a record of a
     shipped implementation. Pass `--no-set-done` to leave `done` unset (it
     remains a human-set state in strict setups; the human already merged, so
     setting done here only records that fact, it grants no merge
     authorization).

State machine (6-label closed vocabulary — aligned with merge_signal.py AC-1):
    spec-waiting-approval -> approved -> build-in-progress -> build-complete
    -> waiting-to-merge -> done
`build-in-progress` is transient (dropped at finalize); `build-complete`
persists; `approved` (set by the spec author) and `done` (set on merge) are the
human gates this detector records, not authorizes. `merged` is a separate
signal label (added by the build/human once the PR is actually merged) distinct
from `done`; `done` means the whole workflow finished end-to-end.

Separation of concerns (NG-4: no new connector/tooling): this script does NOT
call GitHub or the Linear API. The agent is responsible for *confirming* the PR
is merged (`gh pr view <num> --json mergedAt` is non-null) before invoking
`plan`. The pure transforms below are unit-tested without network.

Usage:
  merge_detect.py plan  <issue> [--url <pr_url>] \
        --labels '["build-complete","waiting-to-merge","Improvement"]' \
        --description-file <f>
      # exit 0 = you own it: prints {label_remove, new_description} as an
      #          action payload for the agent to apply, then marks claimed
      # exit 3 = already detected for this issue OR nothing to change (no-op)
  merge_detect.py transform-description   # stdin desc -> stdout new desc (pure)

Reuses scripts/idempotency.py (PROTECTED — call, never modify: NG-2).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IDEMPOTENCY = REPO_ROOT / "scripts" / "idempotency.py"
# Honor JLOOP_ACTION_DIR (same convention as idempotency.py / merge_signal.py)
# so runs/tests can redirect durable action records; default to .factory/actions.
ACTION_DIR = Path(os.environ.get("JLOOP_ACTION_DIR", str(REPO_ROOT / ".factory" / "actions")))

# Closed 6-label state vocabulary (aligned with merge_signal.py AC-1).
STATE_LABELS = [
    "spec-waiting-approval",
    "approved",
    "build-in-progress",
    "build-complete",
    "waiting-to-merge",
    "done",
]

# The callout head merge_signal.py writes when the PR opens.
READY_HEAD = "**✅ Solution Ready For Merge**"
# The callout head this detector writes once the PR is merged (the ask).
MERGED_HEAD = "**✅ Merged / Complete**"
# Matches the ready-callout head line (bold markers + the head text), so a
# re-run leaves an already-flipped callout untouched (idempotent, AC-4).
_READY_RE = re.compile(r"^\*\*✅\s*Solution Ready For Merge\*\*\s*$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# pure transforms (unit-tested, no network)                                   #
# --------------------------------------------------------------------------- #
def flip_callout(description):
    """Return description with the ready callout head replaced by the merged
    head, preserving the PR link line that follows it.

    Idempotent (AC-4): if the callout is already flipped (MERGED_HEAD present)
    or no ready callout exists, the text is returned unchanged.
    """
    desc = description if description is not None else ""
    return _READY_RE.sub(MERGED_HEAD, desc)


def transition_labels(labels, set_done=True):
    """Return (new_labels, added, removed).

    Remove `waiting-to-merge` (the stale build signal). When `set_done`
    (default), add `done` so the issue reaches its terminal human gate.
    `build-complete` is preserved (it records the shipped implementation), as
    is every non-state label. `build-in-progress` is dropped if somehow still
    present (transient). Other stray state labels are dropped so at most
    `build-complete` + `done` remain.
    """
    added, removed = [], []
    out = []
    for lbl in labels:
        if lbl == "waiting-to-merge":
            removed.append(lbl)
            continue
        if lbl == "build-in-progress":  # transient; should not be present, drop
            removed.append(lbl)
            continue
        if lbl in STATE_LABELS:
            if lbl == "build-complete":
                out.append(lbl)  # preserve the implementation record
                continue
            if set_done:
                if lbl != "done":
                    removed.append(lbl)
                continue
            # set_done=False: keep any other state label as-is
            out.append(lbl)
            continue
        out.append(lbl)
    if set_done and "done" not in labels:
        added.append("done")
        out.append("done")
    return out, added, removed


# --------------------------------------------------------------------------- #
# plan (idempotency-guarded action payload for the agent)                      #
# --------------------------------------------------------------------------- #
def _idem(args):
    return subprocess.run([sys.executable, str(IDEMPOTENCY), *args], capture_output=True, text=True)


def cmd_plan(issue, url, labels, description, set_done):
    key = f"merge-detect:{issue}"
    # AC-4: claim BEFORE emitting the action; a replay exits 3 -> no-op.
    if IDEMPOTENCY.exists():
        r = _idem(["claim", key, "--meta", json.dumps({"pr": url})])
        if r.returncode == 3:
            print(json.dumps({"ok": False, "reason": "already-detected", "issue": issue, "key": key}))
            return 3

    orig = description if description is not None else ""
    new_desc = flip_callout(orig)
    new_labels, added, removed = transition_labels(labels, set_done)
    changed = (new_desc != orig) or bool(removed) or bool(added)

    if not changed:
        # Nothing to do — callout already flipped/absent and no label change.
        # Record the no-op so a retry is idempotent, and report exit 3.
        if IDEMPOTENCY.exists():
            _idem(["commit", key, "--meta", json.dumps({"pr": url, "changed": False})])
        print(json.dumps({"ok": False, "reason": "no-change", "issue": issue, "key": key}))
        return 3

    payload = {
        "action": "merge_detect",
        "issue": issue,
        "pr_url": url,
        "labels_add": added,
        "labels_remove": removed,
        "labels_final": new_labels,
        "new_description": new_desc,
        "idempotency_key": key,
    }
    ACTION_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ACTION_DIR), suffix=".merge-detect.json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    if IDEMPOTENCY.exists():
        _idem(["commit", key, "--meta", json.dumps({"pr": url, "file": tmp, "changed": True})])
    print(json.dumps({"ok": True, "file": tmp, "payload": payload}, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser(description="jloop merge-detect (GOL-12)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="emit idempotent callout-flip + label action")
    p.add_argument("issue")
    p.add_argument("--url", default="", help="the merged PR URL (kept in the callout link)")
    p.add_argument("--labels", default="[]", help="current issue labels as JSON array")
    p.add_argument("--description", default="", help="current issue description")
    p.add_argument("--description-file", default="", help="read description from a file")
    p.add_argument(
        "--no-set-done",
        dest="set_done",
        action="store_false",
        help="leave `done` unset (strict setups); by default the detected merge advances the issue to `done`",
    )

    t = sub.add_parser("transform-description", help="pure: stdin desc -> stdout new desc")
    t.add_argument("--url", default="", help="ignored (link line is preserved)")

    a = ap.parse_args()
    if a.cmd == "plan":
        try:
            labels = json.loads(a.labels)
            assert isinstance(labels, list)
        except Exception:
            sys.exit("--labels must be a JSON array")
        desc = a.description
        if a.description_file:
            desc = Path(a.description_file).read_text()
        sys.exit(cmd_plan(a.issue, a.url, labels, desc, a.set_done))
    if a.cmd == "transform-description":
        sys.stdout.write(flip_callout(sys.stdin.read()))
        sys.exit(0)


if __name__ == "__main__":
    main()
