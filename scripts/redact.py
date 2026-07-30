#!/usr/bin/env python3
"""
jloop secret redaction helper (GOL-26).

Centralizes scrubbing of secrets from any text that is about to be printed,
logged, or returned from an external command wrapper (gh, curl, MCP, git with
embedded tokens). The jloop scripts run with a push-only GitHub token (and
sometimes other secrets) in the environment or embedded in command output; a
traceback or echoed command can leak them to stdout/stderr/logs.

Design
------
* One function, `redact_secrets(text)`, used everywhere an external command's
  output or an error message is surfaced.
* Pure / dependency-free so it can be imported by every script without pulling
  in the rest of jloop.
* Patterns covered:
  - `Authorization: Bearer <token>` / `Authorization: <token>`
  - `token=` / `secret=` / `key=` / `password=` / `passwd=` assignments
  - GitHub `x-access-token:<token>` style embedded in URLs
  - generic long base64-ish / high-entropy credential blobs
* Idempotent: redacting already-redacted text is a no-op.
"""

import re
import sys
from pathlib import Path

# Ensure sibling scripts are importable even when this module is run from a
# different working directory (e.g. validate.py / watch.py executed in a tempdir
# by their own test suites). GOL-26.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Authorization headers (with or without the "Bearer" word).
_AUTH_RE = re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)([^\s,;'\"]+)")
# key=value style secrets (token=, secret=, key=, password=, passwd=, ...)
_KV_RE = re.compile(
    r"(?i)((?:"  # case-insensitive; open the group
    r"token|secret|api[_-]?key|key|password|passwd|client[_-]?secret|access[_-]?token"
    r")\s*[=:]\s*)([^\s,;&'\"]+)"  # key=..., then the secret value
)
# embedded credential in a URL: scheme://user:pass@host or x-access-token:TOKEN@
_URLCRED_RE = re.compile(r"(?i)(://|@)([^@/\s:]+):([^@/\s]+)@")
# long high-entropy blobs (>=24 chars of token-ish chars) — catches raw tokens
_BLOB_RE = re.compile(r"\b([A-Za-z0-9_\-]{24,})\b")


def redact_secrets(text):
    """Return `text` with secret-like substrings replaced by ``[REDACTED]``.

    Safe to call on any string; returns "" when input is None/empty.
    """
    if not text:
        return ""
    t = _AUTH_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    t = _KV_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", t)
    t = _URLCRED_RE.sub(lambda m: f"{m.group(1)}[REDACTED]:[REDACTED]@", t)
    t = _BLOB_RE.sub(lambda m: "[REDACTED]" if m.group(1) != "[REDACTED]" else m.group(1), t)
    return t


if __name__ == "__main__":
    import sys as _sys

    data = _sys.stdin.read()
    _sys.stdout.write(redact_secrets(data))
