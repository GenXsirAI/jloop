#!/usr/bin/env python3
"""
Test the skill sync check in validate.py.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

def run_validate(env_vars, cwd):
    """Run validate.py --json and return (exit_code, parsed_json)."""
    env = os.environ.copy()
    env.update(env_vars)
    result = subprocess.run(
        [sys.executable, "validate.py", "--json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    try:
        data = json.loads(result.stdout.strip())
        return result.returncode, data
    except json.JSONDecodeError:
        return result.returncode, {"raw": result.stdout, "stderr": result.stderr}

def test_skill_sync():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        # Set up a minimal jloop-like structure
        #   base/
        #     skills/
        #     .hermes/skills/
        #     scripts/validate.py (copy from repo)
        #     scripts/lease.py, idempotency.py, verify_scope.py, merge_signal.py (dummies)
        #     .factory/contracts/ (dummy contract)
        (base / "skills").mkdir()
        (base / ".hermes" / "skills").mkdir(parents=True)
        (base / "scripts").mkdir()
        (base / ".factory" / "contracts").mkdir(parents=True)

        # Copy the actual validate.py from the repo (resolve repo root from this file)
        repo_scripts = Path(__file__).resolve().parent.parent / "scripts"
        for script_name in ["validate.py", "lease.py", "idempotency.py", "verify_scope.py", "merge_signal.py"]:
            src = repo_scripts / script_name
            dst = base / "scripts" / script_name
            # If the file doesn't exist in the repo (should not happen), create a dummy
            if src.exists():
                dst.write_text(src.read_text())
            else:
                dst.write_text("# dummy\n")

        # Create a dummy contract file to pass the contract check
        (base / ".factory" / "contracts" / "dummy.yaml").write_text(
            "issue: DUMMY\nversion: 1\nacceptance_criteria: [{id: AC-1, text: dummy}]\n"
        )

        # Create a skill in the repo
        repo_skill_dir = base / "skills" / "testskill"
        repo_skill_dir.mkdir(parents=True, exist_ok=True)
        (repo_skill_dir / "SKILL.md").write_text(
            "---\nname: testskill\ndescription: A test skill\n---\n# Test skill\n"
        )

        print("Testing skill sync check...")
        print(f"Using temporary base: {base}")

        # Test 1: In-sync (installed skill matches repo)
        installed_skill_dir = base / ".hermes" / "skills" / "testskill"
        installed_skill_dir.mkdir(parents=True, exist_ok=True)
        (installed_skill_dir / "SKILL.md").write_text(
            "---\nname: testskill\ndescription: A test skill\n---\n# Test skill\n"
        )
        env = {"JLOOP_INSTALLED_SKILLS_DIR": str(base / ".hermes" / "skills")}
        code, data = run_validate(env, base / "scripts")
        print(f"Test 1 (in-sync): exit_code={code}, ok={data.get('ok')}, failures={data.get('failures')}")
        assert code == 0 and data.get("ok") is True, f"Expected success, got {data}"
        print("PASS: In-sync skill passes")

        # Test 2: Out-of-sync (installed skill differs)
        (installed_skill_dir / "SKILL.md").write_text(
            "---\nname: testskill\ndescription: A different skill\n---\n# Different\n"
        )
        code, data = run_validate(env, base / "scripts")
        print(f"Test 2 (out-of-sync): exit_code={code}, ok={data.get('ok')}, failures={data.get('failures')}")
        assert code == 1 and data.get("ok") is False, f"Expected failure, got {data}"
        failures = data.get("failures", [])
        assert any("testskill" in f and "content differs" in f for f in failures), f"Expected skill diff failure, got {failures}"
        print("PASS: Out-of-sync skill fails")

        # Test 3: Missing installed skill
        shutil.rmtree(installed_skill_dir)
        code, data = run_validate(env, base / "scripts")
        print(f"Test 3 (missing): exit_code={code}, ok={data.get('ok')}, failures={data.get('failures')}")
        assert code == 1 and data.get("ok") is False, f"Expected failure, got {data}"
        failures = data.get("failures", [])
        assert any("testskill" in f and "missing in installed" in f for f in failures), f"Expected missing skill failure, got {failures}"
        print("PASS: Missing skill fails")

        # Test 4: Installed-skills directory does not exist (skip)
        env = {"JLOOP_INSTALLED_SKILLS_DIR": str(base / "nonexistent")}
        code, data = run_validate(env, base / "scripts")
        print(f"Test 4 (no installed dir): exit_code={code}, ok={data.get('ok')}, failures={data.get('failures')}")
        assert code == 0 and data.get("ok") is True, f"Expected success (skip), got {data}"
        print("PASS: Missing installed-skills directory skips check")

        print("\nAll tests passed!")

if __name__ == "__main__":
    try:
        test_skill_sync()
        sys.exit(0)
    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)