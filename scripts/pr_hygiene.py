#!/usr/bin/env python3
"""jloop PR-hygiene gate (GOL-20/GOL-21 root-cause fix, recommendation D).

Catches the "dirty working tree pollutes the PR" failure mode before a PR is
opened. Checks the diff that would be pushed (HEAD vs the base branch) for:

  1. committed `.factory/actions/tmp*.json` (auto-generated, never belong in a PR)
  2. committed `.factory/leases/*.json` that are NOT this issue's lease
     (stale/orphan lease records)
  3. committed `.factory/contracts/<ISSUE>.yaml` for an issue other than the one
     being built (orphan contracts)
  4. any file under `.factory/actions/` whose name is not a 40-hex sha1
     (tmp/scratch action records)

Exit codes: 0 = clean; 1 = hygiene violations (must-fix before `gh pr create`).
Prints a JSON report. Designed to be run in the builder's step-7 pre-PR check.

Usage:
  pr_hygiene.py <issue> --base origin/main
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHA1_RE = re.compile(r"^[0-9a-f]{40}\.json$")


def _changed_vs_base(base):
    r = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        # fall back to two-dot
        r = subprocess.run(["git", "diff", "--name-only", base, "HEAD"], cwd=REPO, capture_output=True, text=True)
    return [f for f in r.stdout.splitlines() if f.strip()]


def main():
    ap = argparse.ArgumentParser(description="jloop PR-hygiene gate")
    ap.add_argument("issue", help="Linear issue id being built, e.g. GOL-21")
    ap.add_argument("--base", default="origin/main", help="base branch")
    a = ap.parse_args()

    changed = _changed_vs_base(a.base)
    violations = []

    for f in changed:
        low = f.lower()
        if low.startswith(".factory/actions/"):
            name = Path(f).name
            if name.startswith("tmp") or not SHA1_RE.match(name):
                violations.append(
                    {
                        "type": "TMP-ACTION-RECORD",
                        "file": f,
                        "detail": "auto-generated action record must not be committed",
                    }
                )
        if low.startswith(".factory/leases/") and low.endswith(".json") and not Path(f).name.startswith(a.issue + "."):
            # only the building issue's own lease is allowed
            violations.append(
                {
                    "type": "ORPHAN-LEASE",
                    "file": f,
                    "detail": f"lease record for a different issue (building {a.issue})",
                }
            )
        if low.startswith(".factory/contracts/") and low.endswith(".yaml") and Path(f).stem != a.issue:
            violations.append(
                {
                    "type": "ORPHAN-CONTRACT",
                    "file": f,
                    "detail": f"contract for a different issue (building {a.issue})",
                }
            )

    report = {
        "ok": not violations,
        "issue": a.issue,
        "base": a.base,
        "changed_files": changed,
        "violations": violations,
    }
    print(json.dumps(report, indent=2))
    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
