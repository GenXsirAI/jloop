#!/usr/bin/env python3
"""
jloop graph scope verifier — turn "Other behavior changes: None" from a claim
into a machine-checked fact, using codebase-memory-mcp's knowledge graph.

Finn-loop's builder self-reports scope compliance in prose. jloop verifies it:

  1. Read the machine-readable contract (.factory/contracts/<ISSUE>.yaml) which
     declares `relevant_files` (globs the change is allowed to touch) and
     `protected` (modules/paths that NG-N non-goals forbid changing).
  2. Compute the PR's actually-changed files from git.
  3. FAIL if any changed file is outside `relevant_files` (scope creep) or
     matches `protected` (non-goal violation).
  4. If codebase-memory-mcp is available, additionally check the blast radius:
     use `detect_changes` to find impacted symbols and flag any that live in a
     protected module — catching *transitive* scope violations grep can't see.

Exit codes: 0 = in scope; 2 = scope violation (must-fix); 4 = contract missing.
Prints a JSON report to stdout for the review skill to paste into its verdict.

Env:
  JLOOP_CBM         path to codebase-memory-mcp binary (optional; graph check
                    skipped with a warning if unset/not found)
  JLOOP_CBM_PROJECT project name as returned by `cbm cli list_projects`
"""
import argparse, fnmatch, json, os, subprocess, sys
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError:
    yaml = None


def _load_contract(issue):
    p = Path(".factory/contracts") / f"{issue}.yaml"
    if not p.exists():
        return None, f"contract not found: {p}"
    text = p.read_text()
    if yaml:
        return yaml.safe_load(text), None
    # minimal fallback parser: top-level list keys we care about
    data, cur = {}, None
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith(("-", " ")) and line.rstrip().endswith(":"):
            cur = line.strip()[:-1]; data[cur] = []
        elif line.strip().startswith("-") and cur:
            data[cur].append(line.strip()[1:].strip().strip('"\''))
    return data, None


def _rev_exists(ref):
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                          capture_output=True, text=True).returncode == 0


def _changed_files(base_ref):
    """Files changed vs the merge-base with base_ref. Returns (files, error).

    Never raises: a bad/missing ref or non-git dir yields ([], reason) so the
    caller can emit a clean JSON error instead of crashing the review skill.
    """
    if not _rev_exists("HEAD"):
        return [], "not a git repo or no commits (HEAD missing)"
    if not _rev_exists(base_ref):
        return [], f"base ref not found: {base_ref}"
    mb = subprocess.run(["git", "merge-base", base_ref, "HEAD"],
                        capture_output=True, text=True)
    diff_base = mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() else base_ref
    r = subprocess.run(["git", "diff", "--name-only", f"{diff_base}...HEAD"],
                       capture_output=True, text=True)
    if r.returncode != 0:  # fall back to two-dot, then report if still broken
        r = subprocess.run(["git", "diff", "--name-only", diff_base, "HEAD"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return [], f"git diff failed against {diff_base}: {r.stderr.strip()}"
    return [f for f in r.stdout.splitlines() if f.strip()], None


def _matches_any(path, patterns):
    return any(fnmatch.fnmatch(path, pat) or path.startswith(pat.rstrip("*"))
               for pat in (patterns or []))


def _graph_impact(changed):
    cbm = os.environ.get("JLOOP_CBM")
    project = os.environ.get("JLOOP_CBM_PROJECT")
    if not cbm or not Path(os.path.expanduser(cbm)).exists() or not project:
        return None, "graph check skipped (JLOOP_CBM / JLOOP_CBM_PROJECT not set)"
    try:
        r = subprocess.run([os.path.expanduser(cbm), "cli", "detect_changes",
                            "--project", project],
                           capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout or "{}")
        symbols = [s.get("qualified_name") or s.get("name")
                   for s in data.get("impacted_symbols", [])]
        return symbols, None
    except Exception as e:  # noqa: BLE001
        return None, f"graph check error: {e}"


def main():
    ap = argparse.ArgumentParser(description="jloop graph scope verifier")
    ap.add_argument("issue", help="Linear issue id, e.g. ENG-123")
    ap.add_argument("--base", default=None, help="base branch (default: origin default)")
    a = ap.parse_args()

    contract, err = _load_contract(a.issue)
    if contract is None:
        print(json.dumps({"ok": False, "reason": "no-contract", "detail": err}))
        return 4

    base = a.base or os.environ.get("JLOOP_BASE_BRANCH")
    if not base:
        try:
            base = subprocess.run(
                ["gh", "repo", "view", "--json", "defaultBranchRef",
                 "--jq", ".defaultBranchRef.name"],
                capture_output=True, text=True, check=True).stdout.strip() or "main"
        except Exception:  # noqa: BLE001
            base = "main"
        base = f"origin/{base}"

    changed, diff_err = _changed_files(base)
    if diff_err:
        print(json.dumps({"ok": False, "reason": "diff-error", "detail": diff_err,
                          "issue": a.issue, "base": base}))
        return 4
    allowed = contract.get("relevant_files", [])
    protected = contract.get("protected", [])

    out_of_scope = [f for f in changed if allowed and not _matches_any(f, allowed)]
    protected_hits = [f for f in changed if _matches_any(f, protected)]

    impacted, graph_note = _graph_impact(changed)
    protected_symbol_hits = []
    if impacted:
        for sym in impacted:
            if sym and _matches_any(sym.replace(".", "/"), protected):
                protected_symbol_hits.append(sym)

    violations = []
    if out_of_scope:
        violations.append({"type": "SCOPE-CREEP", "files": out_of_scope,
                           "detail": "changed files outside contract relevant_files"})
    if protected_hits:
        violations.append({"type": "NG-VIOLATION", "files": protected_hits,
                           "detail": "changed files match a protected (non-goal) path"})
    if protected_symbol_hits:
        violations.append({"type": "NG-VIOLATION-TRANSITIVE", "symbols": protected_symbol_hits,
                           "detail": "blast radius reaches a protected module"})

    report = {
        "ok": not violations, "issue": a.issue, "base": base,
        "changed_files": changed, "allowed_globs": allowed, "protected": protected,
        "violations": violations, "graph_note": graph_note,
        "impacted_symbols_count": len(impacted or []),
    }
    print(json.dumps(report, indent=2))
    return 0 if not violations else 2


if __name__ == "__main__":
    sys.exit(main())
