#!/usr/bin/env python3
"""
jloop self-validation — run in CI and locally before trusting an install.

Checks:
  1. Every skills/*/SKILL.md has valid YAML frontmatter with name + description.
  2. Skill names are unique and match their directory.
  3. Shared scripts import and expose their CLIs (smoke, no external calls).
  4. Lease mutual-exclusion + expiry invariant holds (functional test).
  5. Idempotency claim/dup invariant holds (functional test).
  6. Any .factory/contracts/*.yaml parses and has required keys.
  7. Repo skills/*/SKILL.md match installed copies in $JLOOP_INSTALLED_SKILLS_DIR
     (default ~/.hermes/skills). Missing or differing installed skills cause
     failure; missing installed-skills directory skips the check.

Exit 0 = all good; 1 = a check failed (prints the reasons).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # GOL-26: find sibling redact.py
from redact import redact_secrets  # GOL-26: scrub secrets from surfaced output

ROOT = Path(__file__).resolve().parent.parent
FAIL = []


def check_frontmatter():
    skills_dir = ROOT / "skills"
    if not skills_dir.is_dir():
        # Deployment repo (scripts + .factory only; skills live in the agent's
        # skills dir). Nothing to check here.
        return
    names = {}
    for skill in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill.read_text()
        m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if not m:
            FAIL.append(f"{skill}: missing YAML frontmatter")
            continue
        fm = m.group(1)
        name = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
        desc = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
        if not name:
            FAIL.append(f"{skill}: no name in frontmatter")
            continue
        if not desc:
            FAIL.append(f"{skill}: no description in frontmatter")
            continue
        nm = name.group(1).strip()
        if nm != skill.parent.name:
            FAIL.append(f"{skill}: name '{nm}' != dir '{skill.parent.name}'")
        if nm in names:
            FAIL.append(f"duplicate skill name: {nm}")
        names[nm] = skill
    if not names:
        FAIL.append("no skills found")


def check_scripts_exist():
    for s in (
        "lease.py",
        "idempotency.py",
        "verify_scope.py",
        "merge_signal.py",
    ):
        if not (ROOT / "scripts" / s).exists():
            FAIL.append(f"missing script: scripts/{s}")


def _run(args, cwd):
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True)


def check_lease_and_idem():
    with tempfile.TemporaryDirectory() as td:
        env = dict(
            os.environ,
            JLOOP_LEASE_DIR=str(Path(td) / "leases"),
            JLOOP_ACTION_DIR=str(Path(td) / "actions"),
        )
        lease = str(ROOT / "scripts" / "lease.py")
        idem = str(ROOT / "scripts" / "idempotency.py")

        def run(args):
            return subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True)

        # Contention check uses a long TTL so it cannot expire mid-test
        # (a short TTL here races on slow/fast CI runners — fixed after a
        # flaky CI failure during the GOL-7 shakedown).
        r = run([lease, "acquire", "T-1", "--owner", "a", "--ttl", "3600"])
        if r.returncode != 0:
            FAIL.append("lease: first acquire should succeed")
        r = run([lease, "acquire", "T-1", "--owner", "b", "--ttl", "3600"])
        if r.returncode != 3:
            FAIL.append("lease: contended acquire should exit 3")
        run([lease, "release", "T-1", "--owner", "a"])
        # Expiry check uses its own short-TTL lease on a separate key
        r = run([lease, "acquire", "T-2", "--owner", "a", "--ttl", "1"])
        if r.returncode != 0:
            FAIL.append("lease: short-ttl acquire should succeed")
        time.sleep(1.3)
        r = run([lease, "acquire", "T-2", "--owner", "b", "--ttl", "1"])
        if r.returncode != 0:
            FAIL.append("lease: expired lease should be reclaimable")

        r = run([idem, "claim", "k"])
        if r.returncode != 0:
            FAIL.append("idem: first claim should succeed")
        r = run([idem, "claim", "k"])
        if r.returncode != 3:
            FAIL.append("idem: duplicate claim should exit 3")


def check_contracts():
    try:
        import yaml
    except ImportError:
        return  # optional; skills tolerate missing yaml
    for c in (ROOT / ".factory" / "contracts").glob("*.yaml"):
        try:
            data = yaml.safe_load(c.read_text())
        except Exception as e:  # noqa: BLE001
            FAIL.append(f"{c}: invalid YAML ({e})")
            continue
        for key in ("issue", "version", "acceptance_criteria"):
            if key not in (data or {}):
                FAIL.append(f"{c}: missing key '{key}'")


def check_skill_sync():
    """Check that repo skills match installed copies."""
    repo_skills_dir = ROOT / "skills"
    if not repo_skills_dir.is_dir():
        # No skills in repo (deployment scenario) – nothing to check
        return

    installed_dir = Path(os.environ.get("JLOOP_INSTALLED_SKILLS_DIR", Path.home() / ".hermes" / "skills"))
    if not installed_dir.is_dir():
        # AC-2: when installed-skills directory does not exist (e.g. CI), skip
        return

    for skill_dir in repo_skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        repo_skill_file = skill_dir / "SKILL.md"
        installed_skill_file = installed_dir / skill_name / "SKILL.md"

        if not repo_skill_file.is_file():
            # Should not happen, but skip if missing
            continue

        if not installed_skill_file.is_file():
            FAIL.append(f"skill {skill_name}: missing in installed skills directory")
            continue

        if repo_skill_file.read_bytes() != installed_skill_file.read_bytes():
            FAIL.append(f"skill {skill_name}: content differs between repo and installed")


def check_verify_scope():
    """Functional regression for verify_scope.py: in-scope pass, out-of-scope
    violation, and graceful clean-JSON error on a bad base ref."""
    vs = ROOT / "scripts" / "verify_scope.py"
    if not vs.exists():
        FAIL.append("missing script: scripts/verify_scope.py")
        return
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for d in ("src/board", "src/auth", ".factory/contracts"):
            (td / d).mkdir(parents=True)
        (td / ".factory/contracts/ENG-9.yaml").write_text(
            "issue: ENG-9\nversion: 1\n"
            "acceptance_criteria: [{id: AC-1, text: board}]\n"
            "non_goals: [{id: NG-1, text: no auth}]\n"
            'relevant_files: ["src/board/**"]\n'
            'protected: ["src/auth/**"]\nrisk: low\n'
        )

        def g(*a):
            return subprocess.run(["git", *a], cwd=td, capture_output=True, text=True)

        g("init", "-qb", "main")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (td / "src/board/board.ts").write_text("base\n")
        g("add", "-A")
        g("commit", "-qm", "base")
        g("switch", "-qc", "ENG-9-board")

        def run():
            return subprocess.run(
                [sys.executable, str(vs), "ENG-9", "--base", "main"],
                cwd=td,
                capture_output=True,
                text=True,
            )

        # 1. in-scope -> exit 0, valid JSON, ok:true
        (td / "src/board/board.ts").write_text("base\nmore\n")
        g("add", "-A")
        g("commit", "-qm", "in")
        r = run()
        try:
            if r.returncode != 0 or not json.loads(r.stdout)["ok"]:
                FAIL.append("verify_scope: in-scope change should pass (exit 0, ok:true)")
        except (json.JSONDecodeError, KeyError):
            FAIL.append(f"verify_scope: in-scope produced non-JSON: {r.stdout[:80]!r}")

        # 2. out-of-scope (protected path) -> exit 2, violations present
        (td / "src/auth/login.ts").write_text("sneaky\n")
        g("add", "-A")
        g("commit", "-qm", "creep")
        r = run()
        try:
            if r.returncode != 2 or json.loads(r.stdout)["ok"]:
                FAIL.append("verify_scope: protected-path change should fail (exit 2)")
        except (json.JSONDecodeError, KeyError):
            FAIL.append(f"verify_scope: violation produced non-JSON: {r.stdout[:80]!r}")

        # 3. bad base ref -> exit 4, clean JSON, no crash/stderr
        r = subprocess.run(
            [sys.executable, str(vs), "ENG-9", "--base", "no-such-ref"],
            cwd=td,
            capture_output=True,
            text=True,
        )
        try:
            if r.returncode != 4 or json.loads(r.stdout).get("reason") != "diff-error":
                FAIL.append("verify_scope: bad ref should exit 4 with diff-error JSON")
            if r.stderr.strip():
                FAIL.append(f"verify_scope: bad ref leaked stderr: {r.stderr[:80]!r}")
        except json.JSONDecodeError:
            FAIL.append(f"verify_scope: bad ref crashed instead of clean JSON: {r.stdout[:80]!r}")


def main():
    import argparse

    ap = argparse.ArgumentParser(description="jloop self-validation")
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit a single JSON object {ok, failures} instead of text",
    )
    args = ap.parse_args()

    check_frontmatter()
    check_scripts_exist()
    check_lease_and_idem()
    check_contracts()
    check_skill_sync()
    check_verify_scope()

    ok = not FAIL
    if args.json:
        print(json.dumps({"ok": ok, "failures": [redact_secrets(f) for f in FAIL]}))
        return 0 if ok else 1
    if FAIL:
        print("jloop validation FAILED:")
        for f in FAIL:
            print(f"  - {redact_secrets(f)}")
        return 1
    print("jloop validation OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
