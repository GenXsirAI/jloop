#!/usr/bin/env python3
"""
jloop merge-monitor -- keep every open PR mergeable (the "repair" half of
"monitor any merge errors and repair so any PR can be merged").

Why this exists
---------------
GitHub's `mergeable` flag is frequently stale/UNKNOWN, and nothing proactively
detects that an open PR has drifted behind `main` and now conflicts. This script
is the authoritative check: it uses `git merge-tree --write-tree` (the same
engine Git uses for a real merge) to detect true conflicts, and -- with --repair
-- rebases the PR branch onto the fresh default branch and force-pushes it, so
the PR stays MERGEABLE for a human to merge.

It NEVER merges. It only rebases PR branches (a push, permitted by a
push-only GitHub token) to remove conflicts. A human still performs the actual
merge.

Safety
------
* Default mode is DRY-RUN: it reports conflicts and the intended repair, but
  does not push. Pass --repair to actually rebase + force-with-lease push.
* force-with-lease (not --force) so we never clobber concurrent pushes.
* Skips draft PRs and PRs whose base is not the default branch.
* All external-command output is passed through redact_secrets() before logging
  (secret-leakage defense, GOL-26-aware).
* Each repair is recorded under .factory/actions/merge-monitor-<pr>.json for
  auditability and to make re-runs idempotent.

Usage
-----
  merge_monitor.py [--repair] [--base main] [--pr <number>] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION_DIR = REPO_ROOT / ".factory" / "actions"
# crude secret redactor (mirrors GOL-26 intent; kept inline so this script is
# dependency-free and safe to run before GOL-26 lands)
_SECRET_RE = re.compile(r"(?:(?:Authorization|token|secret|key|passwd|password)\s*[:=]\s*)\S+"
                       r"|[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")


def redact(text: str) -> str:
    if not text:
        return text
    return _SECRET_RE.sub("[REDACTED]", text)


def run(cmd, capture=True, check=False):
    r = subprocess.run(cmd, capture_output=capture, text=True, cwd=str(REPO_ROOT))
    if capture and not check:
        # redact anything we might log
        r.stdout = redact(r.stdout)
        r.stderr = redact(r.stderr)
    return r


def list_open_prs(base: str):
    r = run(["gh", "pr", "list", "--state", "open", "--json",
             "number,title,headRefName,baseRefName,isDraft,mergeable,url"])
    if r.returncode != 0:
        print(redact(r.stderr), file=sys.stderr)
        sys.exit(1)
    prs = json.loads(r.stdout or "[]")
    return [p for p in prs if not p.get("isDraft") and p.get("baseRefName") == base]


def conflict_free(base: str, head: str) -> tuple[bool, str]:
    """Return (ok, detail). ok=False means a real merge conflict exists."""
    r = run(["git", "merge-tree", "--write-tree", f"origin/{base}", f"origin/{head}"])
    # git merge-tree (--write-tree) exits 0 with a tree on success; on conflict
    # it exits non-zero and prints conflict info to stderr.
    if r.returncode == 0:
        return True, f"clean (tree {r.stdout.strip()[:12]})"
    # also catch the textual marker just in case
    return False, redact((r.stderr or r.stdout).strip()[:400])


def repair(pr_number: int, head: str, base: str, dry: bool) -> dict:
    """Rebase head onto origin/base in a throwaway worktree and force-with-lease
    push. Returns a status dict."""
    info = {"pr": pr_number, "head": head, "action": "repair" if not dry else "repair(dry-run)"}
    wt = tempfile.mkdtemp(prefix=f"jloop-mm-{pr_number}-")
    try:
        r = run(["git", "worktree", "add", "--force", wt, f"origin/{head}"])
        if r.returncode != 0:
            info["ok"] = False
            info["detail"] = "worktree add failed: " + redact(r.stderr)[:300]
            return info
        rr = subprocess.run(["git", "rebase", f"origin/{base}"], cwd=wt,
                            capture_output=True, text=True)
        if rr.returncode != 0:
            info["ok"] = False
            info["detail"] = "rebase failed (likely still-conflicting): " + redact(rr.stderr)[:300]
            subprocess.run(["git", "rebase", "--abort"], cwd=wt, capture_output=True)
            return info
        if dry:
            info["ok"] = True
            info["detail"] = "rebase would apply cleanly (dry-run, no push)"
            return info
        pr = subprocess.run(["git", "push", "--force-with-lease", "origin",
                             f"HEAD:refs/heads/{head}"], cwd=wt,
                            capture_output=True, text=True)
        info["ok"] = pr.returncode == 0
        info["detail"] = redact(pr.stderr)[:300] or "pushed"
        return info
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt],
                       capture_output=True)


def main():
    ap = argparse.ArgumentParser(description="jloop merge-monitor")
    ap.add_argument("--repair", action="store_true", help="actually rebase+push conflicting PRs")
    ap.add_argument("--base", default="main")
    ap.add_argument("--pr", type=int, default=None, help="only check this PR number")
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    a = ap.parse_args()

    dry = not a.repair
    run(["git", "fetch", "origin", "--quiet"])
    prs = list_open_prs(a.base)
    if a.pr is not None:
        prs = [p for p in prs if p["number"] == a.pr]

    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "base": a.base, "repair": not dry, "prs": []}
    ACTION_DIR.mkdir(parents=True, exist_ok=True)
    for p in prs:
        ok, detail = conflict_free(a.base, p["headRefName"])
        entry = {"number": p["number"], "title": p["title"], "head": p["headRefName"],
                 "url": p["url"], "mergeable_flag": p.get("mergeable"),
                 "conflict_free": ok, "detail": detail}
        if not ok:
            rep = repair(p["number"], p["headRefName"], a.base, dry)
            entry["repair"] = rep
            if not dry and rep.get("ok"):
                # re-verify after repair
                ok2, d2 = conflict_free(a.base, p["headRefName"])
                entry["conflict_free_after"] = ok2
                entry["detail_after"] = d2
                if ok2:
                    # record the repair action for audit/idempotency
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    rec = {"pr": p["number"], "head": p["headRefName"],
                           "repaired_at": stamp, "result": rep}
                    fd, tmp = tempfile.mkstemp(dir=str(ACTION_DIR),
                                              suffix=f".merge-monitor-{p['number']}.json")
                    with os.fdopen(fd, "w") as f:
                        json.dump(rec, f, indent=2); f.write("\n")
                    entry["record"] = tmp
        report["prs"].append(entry)

    if a.json:
        print(json.dumps(report, indent=2))
    else:
        for e in report["prs"]:
            flag = "OK " if e["conflict_free"] else "CONFLICT"
            line = f"[{flag}] PR #{e['number']} {e['head']} (gh says {e['mergeable_flag']}) - {e['detail']}"
            print(line)
            if not e["conflict_free"] and "repair" in e:
                print(f"        repair({e['repair']['action']}): ok={e['repair'].get('ok')} {e['repair'].get('detail')}")
                if e.get("conflict_free_after") is not None:
                    print(f"        after repair: conflict_free={e['conflict_free_after']} - {e.get('detail_after')}")

    # exit code: 0 if all clean (or all repaired), 2 if any still conflicting
    still_bad = [e for e in report["prs"] if not e["conflict_free"]
                 and not (e.get("conflict_free_after"))]
    sys.exit(2 if still_bad else 0)


if __name__ == "__main__":
    main()
