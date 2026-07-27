#!/usr/bin/env python3
"""
jloop merge-signal — the build finalize step (GOL-10).

After jloop-build opens exactly one PR, the issue should visibly enter the
"built, PR open, awaiting human merge" state. This closes the gap where a
finished-but-unmerged issue looked identical to one still being built.

Two side effects, both idempotency-guarded (AC-4, keyed `merge-signal:<issue>`):
  1. LABEL SWAP (AC-2): remove `approved` (and the transient `build-in-progress`),
     keep `build-complete`, add `waiting-to-merge`. After finalize the issue carries
     the `build-complete` + `waiting-to-merge` pair plus any non-state labels
     (Feature/Improvement/Bug), which are always untouched.
  2. DESCRIPTION CALLOUT (AC-3): insert a block on its own line immediately
     below the `## Problem` section (before the next `## ` heading), reading
     exactly:  **✅ Solution Ready For Merge**  → [<url>](<url>)

State machine (six labels, closed vocabulary — AC-1):
    spec-waiting-approval -> approved -> build-in-progress ->
    build-complete -> waiting-to-merge -> done

Design (matches watch.py): this script does NOT call the Linear API directly
(NG-4: no new connector/tooling). It computes the exact desired mutation and
emits an idempotency-guarded action payload the agent executes with its Linear
connector. The pure transform functions below are unit-tested without network.

Usage:
  merge_signal.py plan  <issue> --url <pr_url> --description-file <f> \\
        --labels '["approved","Improvement"]'
      # exit 0 = you own it: prints {label_add, label_remove, new_description}
      #          as an action payload for the agent to apply, then marks claimed
      # exit 3 = already signalled for this issue (idempotent no-op)
  merge_signal.py transform-description   # stdin desc, --url -> stdout new desc (pure)

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
# Honor JLOOP_ACTION_DIR (same convention as idempotency.py) so runs/tests can
# redirect durable action records; default to the committed .factory/actions.
ACTION_DIR = Path(os.environ.get("JLOOP_ACTION_DIR", str(REPO_ROOT / ".factory" / "actions")))

# Closed state-label vocabulary (AC-1). Order == workflow progression.
# The build-aware machine: spec -> approved -> build-in-progress -> build-complete
# -> waiting-to-merge -> done. `build-in-progress` is transient (dropped when
# the build finalizes into build-complete); `build-complete` persists through the
# waiting-to-merge phase as a record that the PR's implementation is done.
STATE_LABELS = [
    "spec-waiting-approval",
    "approved",
    "build-in-progress",
    "build-complete",
    "waiting-to-merge",
    "done",
]
FROM_LABEL = "approved"  # removed at finalize (human approved to build)
TO_LABEL = "waiting-to-merge"  # added at finalize (PR open, awaiting merge)
TRANSIENT_LABEL = "build-in-progress"  # dropped at finalize (build now complete)
PRESERVE_LABEL = "build-complete"  # retained through waiting-to-merge

CALLOUT_HEAD = "**✅ Solution Ready For Merge**"
# Matches a previously-inserted callout block (head + its link line), so a
# re-run updates it in place instead of appending a second one (AC-4).
_CALLOUT_RE = re.compile(
    r"\n?" + re.escape(CALLOUT_HEAD) + r"\n\[[^\]]*\]\([^)]*\)\n",
    re.MULTILINE,
)


# --------------------------------------------------------------------------- #
# pure transforms (unit-tested, no network)                                   #
# --------------------------------------------------------------------------- #
def swap_labels(labels):
    """Return (new_labels, added, removed).

    Finalize the state labels:
      - remove `approved` (FROM_LABEL) and the transient `build-in-progress`;
      - KEEP `build-complete` (PRESERVE_LABEL) so the implementation-done signal
        survives the waiting-to-merge phase;
      - add `waiting-to-merge` (TO_LABEL) if not already present;
      - drop any other state label (e.g. a stray spec-waiting-approval);
      - preserve non-state labels (Feature/Improvement/Bug) in original order.
    """
    added, removed = [], []
    out = []
    for lbl in labels:
        if lbl in STATE_LABELS:
            if lbl == TO_LABEL:
                continue  # already the target; re-appended below
            if lbl in (FROM_LABEL, TRANSIENT_LABEL, PRESERVE_LABEL):
                if lbl != PRESERVE_LABEL:
                    removed.append(lbl)  # approved + build-in-progress are dropped
                if lbl == PRESERVE_LABEL:
                    out.append(lbl)  # build-complete is kept
                continue
            # any other state label (spec-waiting-approval, done) -> drop
            removed.append(lbl)
            continue
        out.append(lbl)  # non-state label preserved
    if TO_LABEL not in labels:
        added.append(TO_LABEL)
    out.append(TO_LABEL)
    return out, added, removed


def callout_block(url):
    return f"{CALLOUT_HEAD}\n[{url}]({url})\n"


def insert_callout(description, url):
    """Insert/refresh the callout immediately below the `## Problem` section.

    Idempotent (AC-4): if a callout already exists anywhere it is removed first,
    then the fresh one (with the current url) is placed right after Problem.
    If there is no `## Problem` heading, place it at the very top as a fallback.
    """
    desc = description if description is not None else ""
    # 1. strip any existing callout so we never stack duplicates
    desc = _CALLOUT_RE.sub("\n", desc)

    block = "\n" + callout_block(url)
    # 2. find the Problem section body end = next top-level heading after it
    m = re.search(r"(^|\n)##\s+Problem\s*\n", desc)
    if not m:
        # fallback: prepend
        return callout_block(url) + "\n" + desc.lstrip("\n")
    start = m.end()
    nxt = re.search(r"\n##\s+", desc[start:])
    insert_at = start + nxt.start() if nxt else len(desc.rstrip("\n"))
    before = desc[:insert_at].rstrip("\n")
    after = desc[insert_at:]
    return before + "\n" + block + ("\n" + after.lstrip("\n") if after.strip() else "\n")


# --------------------------------------------------------------------------- #
# plan (idempotency-guarded action payload for the agent)                      #
# --------------------------------------------------------------------------- #
def _idem(args):
    return subprocess.run([sys.executable, str(IDEMPOTENCY), *args], capture_output=True, text=True)


def cmd_plan(issue, url, labels, description):
    key = f"merge-signal:{issue}"
    # AC-4: claim BEFORE emitting the action; a replay exits 3 -> no-op.
    if IDEMPOTENCY.exists():
        r = _idem(["claim", key, "--meta", json.dumps({"pr": url})])
        if r.returncode == 3:
            print(json.dumps({"ok": False, "reason": "already-signalled", "issue": issue, "key": key}))
            return 3

    new_labels, added, removed = swap_labels(labels)
    new_desc = insert_callout(description, url)
    payload = {
        "action": "merge_signal",
        "issue": issue,
        "pr_url": url,
        "labels_add": added,
        "labels_remove": removed,
        "labels_final": new_labels,
        "new_description": new_desc,
        "idempotency_key": key,
    }
    ACTION_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ACTION_DIR), suffix=".merge-signal.json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    if IDEMPOTENCY.exists():
        _idem(["commit", key, "--meta", json.dumps({"pr": url, "file": tmp})])
    print(json.dumps({"ok": True, "file": tmp, "payload": payload}, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser(description="jloop merge-signal finalize (GOL-10)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="emit idempotent label-swap + callout action")
    p.add_argument("issue")
    p.add_argument("--url", required=True, help="the open PR URL")
    p.add_argument("--labels", default="[]", help="current issue labels as JSON array")
    p.add_argument("--description", default="", help="current issue description")
    p.add_argument("--description-file", default="", help="read description from a file")

    t = sub.add_parser("transform-description", help="pure: stdin desc -> stdout new desc")
    t.add_argument("--url", required=True)

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
        sys.exit(cmd_plan(a.issue, a.url, labels, desc))
    if a.cmd == "transform-description":
        sys.stdout.write(insert_callout(sys.stdin.read(), a.url))
        sys.exit(0)


if __name__ == "__main__":
    main()
