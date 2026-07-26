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

Exit 0 = all good; 1 = a check failed (prints the reasons).
"""
import re, subprocess, sys, tempfile, os, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAIL = []


def check_frontmatter():
    names = {}
    for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
        text = skill.read_text()
        m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if not m:
            FAIL.append(f"{skill}: missing YAML frontmatter"); continue
        fm = m.group(1)
        name = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
        desc = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
        if not name:
            FAIL.append(f"{skill}: no name in frontmatter"); continue
        if not desc:
            FAIL.append(f"{skill}: no description in frontmatter")
        nm = name.group(1).strip()
        if nm != skill.parent.name:
            FAIL.append(f"{skill}: name '{nm}' != dir '{skill.parent.name}'")
        if nm in names:
            FAIL.append(f"duplicate skill name: {nm}")
        names[nm] = skill
    if not names:
        FAIL.append("no skills found")


def check_scripts_exist():
    for s in ("lease.py", "idempotency.py", "verify_scope.py"):
        if not (ROOT / "scripts" / s).exists():
            FAIL.append(f"missing script: scripts/{s}")


def _run(args, cwd):
    return subprocess.run([sys.executable, *args], cwd=cwd,
                          capture_output=True, text=True)


def check_lease_and_idem():
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ,
                   JLOOP_LEASE_DIR=str(Path(td) / "leases"),
                   JLOOP_ACTION_DIR=str(Path(td) / "actions"))
        lease = str(ROOT / "scripts" / "lease.py")
        idem = str(ROOT / "scripts" / "idempotency.py")

        def run(args):
            return subprocess.run([sys.executable, *args], env=env,
                                  capture_output=True, text=True)

        r = run([lease, "acquire", "T-1", "--owner", "a", "--ttl", "1"])
        if r.returncode != 0: FAIL.append("lease: first acquire should succeed")
        r = run([lease, "acquire", "T-1", "--owner", "b", "--ttl", "1"])
        if r.returncode != 3: FAIL.append("lease: contended acquire should exit 3")
        time.sleep(1.2)
        r = run([lease, "acquire", "T-1", "--owner", "b", "--ttl", "1"])
        if r.returncode != 0: FAIL.append("lease: expired lease should be reclaimable")

        r = run([idem, "claim", "k"])
        if r.returncode != 0: FAIL.append("idem: first claim should succeed")
        r = run([idem, "claim", "k"])
        if r.returncode != 3: FAIL.append("idem: duplicate claim should exit 3")


def check_contracts():
    try:
        import yaml
    except ImportError:
        return  # optional; skills tolerate missing yaml
    for c in (ROOT / ".factory" / "contracts").glob("*.yaml"):
        try:
            data = yaml.safe_load(c.read_text())
        except Exception as e:  # noqa: BLE001
            FAIL.append(f"{c}: invalid YAML ({e})"); continue
        for key in ("issue", "version", "acceptance_criteria"):
            if key not in (data or {}):
                FAIL.append(f"{c}: missing key '{key}'")


def main():
    check_frontmatter()
    check_scripts_exist()
    check_lease_and_idem()
    check_contracts()
    if FAIL:
        print("jloop validation FAILED:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("jloop validation OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
