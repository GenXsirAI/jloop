#!/usr/bin/env python3
"""Tests for scripts/redact.py (GOL-26)."""
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("redact", str(REPO_ROOT / "scripts" / "redact.py"))
redact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(redact)
redact_secrets = redact.redact_secrets


def test_authorization_header():
    s = 'Authorization: Bearer ghp_1234567890abcdef'
    assert "ghp_1234567890abcdef" not in redact_secrets(s)
    assert "[REDACTED]" in redact_secrets(s)


def test_kv_secret():
    s = "token=supersecretvalue123 password=hunter2"
    out = redact_secrets(s)
    assert "supersecretvalue123" not in out
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_url_credential():
    s = "https://x-access-token:abc123@github.com/x/y.git"
    out = redact_secrets(s)
    assert "abc123" not in out
    assert "[REDACTED]" in out


def test_preserves_normal_text():
    s = "merge conflict in scripts/lease.py at line 42"
    assert redact_secrets(s) == s


def test_idempotent():
    s = "Authorization: Bearer xyz"
    once = redact_secrets(s)
    assert redact_secrets(once) == once


def test_empty():
    assert redact_secrets("") == ""
    assert redact_secrets(None) == ""


if __name__ == "__main__":
    for fn in (test_authorization_header, test_kv_secret, test_url_credential,
               test_preserves_normal_text, test_idempotent, test_empty):
        fn()
        print(f"OK {fn.__name__}")
    print("ALL PASS")
