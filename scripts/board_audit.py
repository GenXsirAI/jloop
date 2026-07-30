#!/usr/bin/env python3
"""jloop board-hygiene audit (GOL-28).

Detects and (with --apply) fixes state/label contradictions so the Linear board
never lies about an issue's true status. The pure detection/fix-planning
functions (audit_issue, collect_fixes) are unit-tested; the CLI queries Linear
and applies only the SAFE fixes.

Honesty invariants checked
--------------------------
  R1  Duplicate/Canceled state must carry NO build labels
      (approved, build-in-progress, build-complete, waiting-to-merge).
      -> remove those labels.
  R2  An issue labelled `done` or `merged` MUST be in state `Done`.
      -> set state to Done.
  R3  A done/merged issue must not also carry pipeline labels
      (approved, build-in-progress, build-complete, waiting-to-merge).
      -> remove them.
  R4* waiting-to-merge but no open/merged PR linked -> ADVISORY (no auto-fix).
  R5* build-in-progress but no active lease file -> ADVISORY (no auto-fix).

  * advisory findings are reported but NEVER auto-applied (need human judgment).

A "fix" finding is always mechanical and reversible (label remove / state set).
The merge-label cron (scripts/merge_label_cron.sh) reconciles waiting-to-merge ->
Done on merge; this audit is the broader safety net that catches the cases the
cron's scope misses (Duplicate+approved, stale In Progress state, lingering
pipeline labels) and reports the advisory ones.

Usage:
  board_audit.py            # dry-run: print report, exit 0
  board_audit.py --apply    # apply safe (R1-R3) fixes
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEAM_ID = "88580444-fb79-4775-aa5c-a6eac1da51bd"

# Build/state-label vocabulary (the closed machine from merge_signal.py).
BUILD_LABELS = {"approved", "build-in-progress", "build-complete", "waiting-to-merge"}
PIPELINE_LABELS = BUILD_LABELS  # alias for clarity
DONE_LABELS = {"done", "merged"}
TERMINAL_STATES = {"Duplicate", "Canceled"}


# --------------------------------------------------------------------------- #
# Pure detection (unit-tested, no network)                                    #
# --------------------------------------------------------------------------- #
def audit_issue(issue):
    """Return a list of Finding dicts for one issue.

    `issue` keys: identifier, state (str state name), labels (list[str]),
    has_open_pr (bool), has_lease (bool).
    """
    ident = issue.get("identifier", "?")
    state = issue.get("state", "")
    labels = set(issue.get("labels", []))
    has_open_pr = issue.get("has_open_pr", False)
    has_lease = issue.get("has_lease", False)
    findings = []

    # R1: terminal state must not carry build labels.
    if state in TERMINAL_STATES:
        bad = sorted(labels & BUILD_LABELS)
        if bad:
            findings.append(
                {
                    "rule": "R1",
                    "severity": "fix",
                    "identifier": ident,
                    "detail": f"state '{state}' but carries build labels {bad}",
                    "remove_labels": bad,
                }
            )

    # R2: done/merged label implies state Done.
    if labels & DONE_LABELS and state != "Done":
        findings.append(
            {
                "rule": "R2",
                "severity": "fix",
                "identifier": ident,
                "detail": f"labels {sorted(labels & DONE_LABELS)} present but state is '{state}'",
                "set_state": "Done",
            }
        )

    # R3: done/merged issue must not carry pipeline labels.
    if labels & DONE_LABELS:
        bad = sorted(labels & PIPELINE_LABELS)
        if bad:
            findings.append(
                {
                    "rule": "R3",
                    "severity": "fix",
                    "identifier": ident,
                    "detail": f"done/merged but still carries pipeline labels {bad}",
                    "remove_labels": bad,
                }
            )

    # R4 (advisory): waiting-to-merge but no linked open/merged PR.
    if "waiting-to-merge" in labels and not has_open_pr:
        findings.append(
            {
                "rule": "R4",
                "severity": "advisory",
                "identifier": ident,
                "detail": "waiting-to-merge but no open/merged PR found",
                "remove_labels": [],
            }
        )

    # R5 (advisory): build-in-progress but no active lease.
    if "build-in-progress" in labels and not has_lease:
        findings.append(
            {
                "rule": "R5",
                "severity": "advisory",
                "identifier": ident,
                "detail": "build-in-progress but no active lease file",
                "remove_labels": [],
            }
        )

    return findings


def collect_fixes(findings):
    """Group a finding list into safe fix mutations per issue.

    Returns dict: identifier -> {"remove_labels": [..], "set_state": str|None}.
    Advisory findings are excluded.
    """
    fixes = {}
    for f in findings:
        if f["severity"] != "fix":
            continue
        ident = f["identifier"]
        slot = fixes.setdefault(ident, {"remove_labels": [], "set_state": None})
        for lab in f.get("remove_labels", []):
            if lab not in slot["remove_labels"]:
                slot["remove_labels"].append(lab)
        if f.get("set_state"):
            slot["set_state"] = f["set_state"]
    return fixes


# --------------------------------------------------------------------------- #
# GraphQL helpers (CLI only)                                                  #
# --------------------------------------------------------------------------- #
def _lgql(query, variables):
    key = os.environ["LINEAR_API_KEY"]
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=30))  # noqa: S310


def fetch_issues():
    """Return raw issue dicts (identifier, state, labels, description)."""
    q = """query($tid:String!){
      team(id:$tid){
        issues(first:100){
          nodes{ id identifier title state{ name } labels{ nodes{ name } } description }
        }
      }
    }"""
    d = _lgql(q, {"tid": TEAM_ID})
    return d["data"]["team"]["issues"]["nodes"]


def resolve_label_ids(_dummy=None):
    q = """query($tid:String!){ team(id:$tid){ labels(first:60){ nodes{ id name } } } }"""
    d = _lgql(q, {"tid": TEAM_ID})
    return {lab["name"]: lab["id"] for lab in d["data"]["team"]["labels"]["nodes"]}


def resolve_state_id(name):
    q = """query($tid:String!){ team(id:$tid){ states(first:60){ nodes{ id name } } } }"""
    d = _lgql(q, {"tid": TEAM_ID})
    return {s["name"]: s["id"] for s in d["data"]["team"]["states"]["nodes"]}.get(name)


def apply_fix(issue_id, remove_label_names, set_state_id):
    q = """mutation($id:String!,$labels:[String!],$state:String){
      issueUpdate(id:$id, input:{ labelIds:$labels, stateId:$state }){ success }
    }"""
    cur = _lgql("""query($iid:String!){ issue(id:$iid){ labels{ nodes{ id name } } } }""", {"iid": issue_id})["data"][
        "issue"
    ]["labels"]["nodes"]
    keep = [lab["id"] for lab in cur if lab["name"] not in set(remove_label_names)]
    if set_state_id:
        return _lgql(q, {"id": issue_id, "labels": keep, "state": set_state_id})
    q2 = """mutation($id:String!,$labels:[String!]){
      issueUpdate(id:$id, input:{ labelIds:$labels }){ success }
    }"""
    return _lgql(q2, {"id": issue_id, "labels": keep})


def has_open_pr(ident):
    """Check for an open or merged PR referencing this issue in:body."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--search", ident + " in:body", "--json", "number,state"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        prs = json.loads(out) if out.strip() else []
        return any(p.get("state") in ("OPEN", "MERGED") for p in prs)
    except Exception:
        return False


def has_lease(ident):
    lease = REPO_ROOT / ".factory" / "leases" / (f"{ident}.json")
    return lease.exists()


# --------------------------------------------------------------------------- #
# Reporting                                                                   #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="jloop board-hygiene audit (GOL-28)")
    ap.add_argument("--apply", action="store_true", help="apply safe (R1-R3) fixes")
    a = ap.parse_args()

    raw = fetch_issues()

    all_findings = []
    issues_for_fix = []
    for it in raw:
        ident = it["identifier"]
        state = it["state"]["name"]
        labels = [lab["name"] for lab in it["labels"]["nodes"]]
        issue = {
            "identifier": ident,
            "state": state,
            "labels": labels,
            "has_open_pr": has_open_pr(ident),
            "has_lease": has_lease(ident),
        }
        fs = audit_issue(issue)
        if fs:
            all_findings.extend(fs)
            issues_for_fix.append((it, fs))

    fixes = collect_fixes(all_findings)
    fix_count = sum(1 for v in fixes.values() if v["remove_labels"] or v["set_state"])
    advisory = [f for f in all_findings if f["severity"] == "advisory"]

    print("=== board-hygiene audit ===")
    print(
        f"issues scanned: {len(raw)} | contradictions: {len(all_findings)} "
        f"(fix: {fix_count}, advisory: {len(advisory)})"
    )
    for f in all_findings:
        tag = "FIX " if f["severity"] == "fix" else "ADV "
        print(f"  [{tag}] {f['rule']} {f['identifier']}: {f['detail']}")

    if a.apply and fixes:
        print("--- applying fixes ---")
        label_name_to_id = resolve_label_ids()
        for it, _fs in issues_for_fix:
            ident = it["identifier"]
            fix = fixes.get(ident)
            if not fix:
                continue
            set_state_id = resolve_state_id(fix["set_state"]) if fix["set_state"] else None
            res = apply_fix(
                it["id"],
                [n for n in fix["remove_labels"] if n in label_name_to_id],
                set_state_id,
            )
            print(f"  APPLIED {ident}: {res.get('data', {}).get('issueUpdate')}")
        print("--- done ---")
    elif a.apply:
        print("--- nothing to fix ---")

    sys.exit(0)


if __name__ == "__main__":
    main()
