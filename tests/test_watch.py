#!/usr/bin/env python3
"""
Unit tests for scripts/watch.py (GOL-9 upstream-drift watchdog).

Run:  python3 tests/test_watch.py        (standalone runner, no pytest needed)
   or: python3 -m pytest tests/test_watch.py -q   (if pytest installed)

Design: every network call (git ls-remote) is monkeypatched to canned data,
and all durable state (registry + idempotency action records) is redirected to
temp dirs via env vars / monkeypatching, so tests never touch the real
.factory/ tree or the network.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WATCH = REPO / "scripts" / "watch.py"


def _load():
    spec = importlib.util.spec_from_file_location("watch", str(WATCH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk_skill(root, name, url):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"source {url}\n")
    return d


# canned ls-remote responses keyed by repo url
def fake_ls_remote(repo):
    table = {
        "https://github.com/foo/alpha": ("main", "a" * 40, {"v1.0.0": "a" * 40}, None),
        "https://github.com/foo/beta": ("main", "b" * 40, {}, None),
        "https://github.com/finna/Finn-loop": ("main", "7" * 40, {}, None),
        "https://github.com/vllm-project/vllm": ("main", "c" * 40, {"v0.26.0": "c" * 40}, None),
    }
    if repo in table:
        return table[repo]
    return None, None, None, f"Repository not found: {repo}"


def _patch_common(w, tmp_path, monkeypatch):
    reg = tmp_path / "watch.yaml"
    actions = tmp_path / "actions"; actions.mkdir()
    monkeypatch.setattr(w, "WATCH_YAML", reg)
    monkeypatch.setattr(w, "ACTION_DIR", actions)
    monkeypatch.setattr(w, "IDEMPOTENCY", REPO / "scripts" / "idempotency.py")
    monkeypatch.setattr(w, "ls_remote", fake_ls_remote)
    monkeypatch.setenv("JLOOP_ACTION_DIR", str(actions))
    return reg, actions


def test_backfill_records_and_skips(tmp_path, monkeypatch):
    w = _load()
    reg, _ = _patch_common(w, tmp_path, monkeypatch)
    skills = tmp_path / "skills"; skills.mkdir()
    _mk_skill(skills, "alpha", "https://github.com/foo/alpha")
    _mk_skill(skills, "beta", "https://github.com/foo/beta")
    monkeypatch.setattr(w, "HERMES_SKILLS", skills)
    monkeypatch.setattr(w, "JLOOP_SKILLS", tmp_path / "js_none")

    answers = {"alpha": ("https://github.com/foo/alpha", "main"), "beta": None}
    w.cmd_backfill(lambda n, p, d: answers.get(n))

    data = w.load_registry()
    names = {s["name"] for s in data["skills"]}
    assert names == {"alpha"}, names
    assert "beta" not in names
    # alpha seeded with a real (canned) sha
    assert data["skills"][0]["last_seen_sha"] == "a" * 40


def test_backfill_skips_unresolvable_repo(tmp_path, monkeypatch):
    w = _load()
    reg, _ = _patch_common(w, tmp_path, monkeypatch)
    skills = tmp_path / "skills"; skills.mkdir()
    _mk_skill(skills, "ghost", "https://github.com/does-not-exist-xyz/ghost")
    monkeypatch.setattr(w, "HERMES_SKILLS", skills)
    monkeypatch.setattr(w, "JLOOP_SKILLS", tmp_path / "js_none")
    w.cmd_backfill(lambda n, p, d: ("https://github.com/does-not-exist-xyz/ghost", "main"))
    data = w.load_registry()
    assert data["skills"] == [], data["skills"]


def test_sha_normalized_to_string(tmp_path, monkeypatch):
    w = _load()
    reg, _ = _patch_common(w, tmp_path, monkeypatch)
    # all-zero hex is parsed by YAML as int 0 -> must be reset to "" (corrupt,
    # re-resolved on next run), NOT coerced to the lossy string "0".
    reg.write_text(
        "skills:\n"
        "  - name: x\n    repo: https://github.com/foo/x\n"
        "    ref: main\n"
        "    last_seen_sha: 0000000000000000000000000000000000000000\n"
        "    last_seen_tag: ''\n")
    data = w.load_registry()
    assert data["skills"][0]["last_seen_sha"] == "", repr(data["skills"][0]["last_seen_sha"])


def test_sha_roundtrip_preserves_all_digit_sha(tmp_path, monkeypatch):
    """A 40-char all-digit SHA must survive save -> reload as a string."""
    w = _load()
    reg, _ = _patch_common(w, tmp_path, monkeypatch)
    data = {"skills": [{"name": "y", "repo": "https://github.com/foo/y",
                        "ref": "main", "last_seen_sha": "1" * 40,
                        "last_seen_tag": "v1.0.0"}]}
    w.save_registry(data)
    raw = reg.read_text()
    assert "'" in raw and "1" * 40 in raw, "SHA must be single-quoted in YAML"
    reloaded = w.load_registry()
    assert reloaded["skills"][0]["last_seen_sha"] == "1" * 40
    assert isinstance(reloaded["skills"][0]["last_seen_sha"], str)


def test_detect_emits_payload_and_advances(tmp_path, monkeypatch):
    w = _load()
    reg, actions = _patch_common(w, tmp_path, monkeypatch)
    # seed a stale but well-formed 40-char sha via save_registry (which quotes
    # the SHA so YAML never numeric-parses it)
    w.save_registry({"skills": [{
        "name": "finna-Finn-loop", "path": "/tmp/fake",
        "repo": "https://github.com/finna/Finn-loop", "ref": "main",
        "last_seen_sha": "1" * 40, "last_seen_tag": ""}]})
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        w.cmd_run(dry_run=True)
    out = buf.getvalue()
    assert "DRIFT" in out and "payload" in out
    data = w.load_registry()
    assert data["skills"][0]["last_seen_sha"] == "7" * 40
    # dry run must not create any durable action record
    assert list(actions.glob("*.json")) == []


def test_idempotency_blocks_duplicate_filing(tmp_path, monkeypatch):
    w = _load()
    reg, actions = _patch_common(w, tmp_path, monkeypatch)
    w.save_registry({"skills": [{
        "name": "finna-Finn-loop", "path": "/tmp/fake",
        "repo": "https://github.com/finna/Finn-loop", "ref": "main",
        "last_seen_sha": "1" * 40, "last_seen_tag": ""}]})
    w.cmd_run(dry_run=False)  # files drift issue (idempotency claim created)
    drift = [json.loads(p.read_text()) for p in actions.glob("*.json")
             if "drift:GOL-9" in json.loads(p.read_text()).get("key", "")]
    assert len(drift) == 1, drift

    w.cmd_run(dry_run=False)  # registry advanced -> no drift, no new claim
    drift2 = [json.loads(p.read_text()) for p in actions.glob("*.json")
              if "drift:GOL-9" in json.loads(p.read_text()).get("key", "")]
    assert len(drift2) == 1, "second run must not create another drift record"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            class MP:
                def setattr(self, obj, k, v):
                    setattr(obj, k, v)
                def setenv(self, k, v):
                    os.environ[k] = v
            mp = MP()
            t(tmp_path=Path(tempfile.mkdtemp()), monkeypatch=mp)
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failed else 0)
