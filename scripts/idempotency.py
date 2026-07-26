#!/usr/bin/env python3
"""
jloop idempotency guard — make every external side effect exactly-once.

Finn-loop can create duplicate PRs/comments when a loop retries or two sessions
race. jloop requires a caller to CLAIM an action key in durable storage BEFORE
performing the external write (PR create, label change, comment, merge). A
replayed action finds the key already claimed and becomes a no-op.

Keys are content-addressed on the tuple that must be unique, e.g.:
  pr-create:TEAM-123               (one PR per issue)
  review:TEAM-123:<head_sha>       (one verdict per reviewed commit)
  comment:TEAM-123:<kind>:<sha>    (one comment of a kind per commit)

Usage:
  idempotency.py claim  <key> [--meta '{"pr":42}']   # exit 0 = you own it, go
                                                      # exit 3 = already done, skip
  idempotency.py commit <key> [--meta '{"url":"..."}']  # mark completed after success
  idempotency.py status <key>

Records live in .factory/actions/<sha1>.json and should be committed so the
"already done" fact is durable across crashes and shared between workers.
"""
import argparse, hashlib, json, os, sys, time, tempfile
from pathlib import Path

ACT_DIR = Path(os.environ.get("JLOOP_ACTION_DIR", ".factory/actions"))


def _rec(key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()
    return ACT_DIR / f"{h}.json"


def _read(p):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True); f.write("\n")
    os.replace(tmp, p)


def claim(key, meta):
    p = _rec(key); p.parent.mkdir(parents=True, exist_ok=True)
    existing = _read(p)
    if existing is not None:
        print(json.dumps({"ok": False, "reason": "already-claimed",
                          "state": existing.get("state"), "record": existing}))
        return 3
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        print(json.dumps({"ok": False, "reason": "race-lost"})); return 3
    rec = {"key": key, "state": "claimed", "claimed_at": int(time.time()), "meta": meta or {}}
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f, indent=2, sort_keys=True); f.write("\n")
    print(json.dumps({"ok": True, "record": rec})); return 0


def commit(key, meta):
    p = _rec(key); rec = _read(p)
    if rec is None:
        rec = {"key": key}
    rec["state"] = "committed"; rec["committed_at"] = int(time.time())
    if meta:
        rec.setdefault("meta", {}).update(meta)
    _write(p, rec)
    print(json.dumps({"ok": True, "record": rec})); return 0


def status(key):
    rec = _read(_rec(key))
    print(json.dumps({"exists": rec is not None, "record": rec})); return 0


def main():
    ap = argparse.ArgumentParser(description="jloop idempotency guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("claim", "commit"):
        s = sub.add_parser(c); s.add_argument("key"); s.add_argument("--meta", default="")
    s = sub.add_parser("status"); s.add_argument("key")
    a = ap.parse_args()
    meta = {}
    if getattr(a, "meta", ""):
        try: meta = json.loads(a.meta)
        except json.JSONDecodeError: sys.exit("--meta must be JSON")
    if a.cmd == "claim":  sys.exit(claim(a.key, meta))
    if a.cmd == "commit": sys.exit(commit(a.key, meta))
    if a.cmd == "status": sys.exit(status(a.key))


if __name__ == "__main__":
    main()
