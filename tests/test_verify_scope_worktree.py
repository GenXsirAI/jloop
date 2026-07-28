#!/usr/bin/env python3
"""
Regression test for scripts/verify_scope.py contract loading from inside a
linked git worktree (GOL-12 scope verification hardening).

The build loop may run INSIDE a linked worktree whose own toplevel is a /tmp
path (e.g. GiLoop builds in /tmp/<issue>). Durable .factory state lives only in
the main repo and is typically git-ignored, so it is NOT checked out into the
worktree. A naive ``Path(".factory/contracts")`` (relative to cwd) therefore
FAILS to find the contract when invoked from the worktree:

    contract not found: .factory/contracts/ENG-1.yaml

This test reproduces that exact condition and asserts verify_scope resolves the
MAIN repo root via ``git rev-parse --git-common-dir`` so the contract loads.

Run:  python3 tests/test_verify_scope_worktree.py
   or: python3 -m pytest tests/test_verify_scope_worktree.py -q

Self-contained: creates temp git repos, never touches the real .factory tree.
Compatible with both the hand-rolled harness (python3 <file>) and pytest
(each test loads the module itself; no pytest-fixture parameters).
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "verify_scope.py"

PASS, FAIL = [], []


def _load():
    spec = importlib.util.spec_from_file_location("verify_scope", str(MOD))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ok(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


def _git(repo, *args, check=True):
    r = subprocess.run(["git", *args], cwd=str(repo),
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r


def _make_repo_with_ignored_factory(root):
    """Create a git repo with a GIT-IGNORED .factory/contracts tree.

    .factory being ignored is the real-world condition: durable state is never
    committed, so it is absent from any checked-out worktree.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@jloop.local")
    _git(root, "config", "user.name", "jloop-test")
    (root / ".gitignore").write_text(".factory/\n")
    (root / ".factory" / "contracts").mkdir(parents=True)
    (root / ".factory" / "contracts" / "ENG-1.yaml").write_text(
        "relevant_files:\n  - src/*.py\nprotected:\n  - core/secrets.py\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def test_loads_from_main_repo():
    """Baseline: contract loads when cwd IS the main repo.

    _repo_root() resolves via git (cwd-relative), so the test must chdir into
    the repo under test rather than relying on the test runner's cwd.
    """
    m = _load()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "mainrepo"
        _make_repo_with_ignored_factory(root)
        old = os.getcwd()
        try:
            os.chdir(str(root))
            data, err = m._load_contract("ENG-1")
        finally:
            os.chdir(old)
        ok("loads contract from main repo root", data is not None and err is None)
        ok("parses relevant_files + protected",
           data.get("relevant_files") == ["src/*.py"]
           and data.get("protected") == ["core/secrets.py"])


def test_loads_from_linked_worktree():
    """Regression: contract must load when cwd is a LINKED worktree.

    Before the fix this FAILS with 'contract not found' because the worktree
    does not contain the git-ignored .factory tree, and a cwd-relative path
    misses it.
    """
    m = _load()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "mainrepo"
        _make_repo_with_ignored_factory(root)
        wt = Path(tmp) / "wt"
        _git(root, "worktree", "add", "-q", str(wt))
        # Sanity: the worktree must NOT contain the ignored .factory state.
        ok("worktree omits git-ignored .factory (precondition)",
           not (wt / ".factory").exists())
        # Run _load_contract with cwd = worktree.
        old = os.getcwd()
        try:
            os.chdir(str(wt))
            data, err = m._load_contract("ENG-1")
        finally:
            os.chdir(old)
        ok("loads contract from inside linked worktree",
           data is not None and err is None)
        ok("worktree load parses contract fields",
           data is not None
           and data.get("relevant_files") == ["src/*.py"]
           and data.get("protected") == ["core/secrets.py"])


def test_missing_contract_still_reports_error():
    """Negative control: a genuinely absent contract still reports not-found
    (the fix must not mask real 'no contract' conditions).
    """
    m = _load()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "mainrepo"
        root.mkdir(parents=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@jloop.local")
        _git(root, "config", "user.name", "jloop-test")
        _git(root, "commit", "-qm", "init", "--allow-empty")
        data, err = m._load_contract("ENG-9")
        ok("absent contract -> (None, error)", data is None and err)


if __name__ == "__main__":
    test_loads_from_main_repo()
    test_loads_from_linked_worktree()
    test_missing_contract_still_reports_error()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
