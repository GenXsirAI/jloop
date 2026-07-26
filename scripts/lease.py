#!/usr/bin/env python3
"""
jloop lease manager — a real, atomic, expiring claim lock.

Finn-loop's weakness: the Linear assignee "is not an atomic lock between
simultaneous sessions." jloop fixes this with a durable, renewable file lease
that is committed to the repo (durable state) and acquired with an atomic
O_CREAT|O_EXCL write (no two workers can create the same lease file).

State machine per issue: (no file) -> leased -> running -> released/expired.
A crashed worker's lease EXPIRES and the issue automatically requeues.

Usage:
  lease.py acquire  TEAM-123 --owner "$JLOOP_WORKER_ID" [--ttl 1800]
  lease.py renew    TEAM-123 --owner "$JLOOP_WORKER_ID" [--ttl 1800]
  lease.py release  TEAM-123 --owner "$JLOOP_WORKER_ID"
  lease.py status   TEAM-123
  lease.py reap                      # release all expired leases, print reaped ids

Exit codes: 0 success; 3 lease held by someone else / not expired; 4 not found;
5 owner mismatch. Non-zero always means "do not proceed".

Leases live in .factory/leases/<ISSUE>.json and are meant to be committed so the
claim survives a process crash and is visible to every worker and to CI.
"""
import argparse, json, os, sys, time, tempfile
from pathlib import Path

LEASE_DIR = Path(os.environ.get("JLOOP_LEASE_DIR", ".factory/leases"))
DEFAULT_TTL = int(os.environ.get("JLOOP_LEASE_TTL", "1800"))  # 30 min


def _path(issue: str) -> Path:
    safe = "".join(c for c in issue if c.isalnum() or c in "-_").upper()
    if not safe:
        sys.exit("invalid issue id")
    return LEASE_DIR / f"{safe}.json"


def _now() -> int:
    return int(time.time())


def _read(p: Path):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _atomic_write(p: Path, data: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, p)  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _expired(lease: dict) -> bool:
    return _now() >= int(lease.get("expires_at", 0))


def acquire(issue, owner, ttl):
    p = _path(issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = _read(p)
    if existing and not _expired(existing) and existing.get("owner") != owner:
        print(json.dumps({"ok": False, "reason": "held", "by": existing.get("owner"),
                          "expires_at": existing.get("expires_at")}))
        return 3
    # If expired or ours, we take it. Use exclusive create when no live file.
    lease = {
        "issue": _path(issue).stem, "owner": owner, "state": "leased",
        "acquired_at": _now(), "expires_at": _now() + ttl, "renewals": 0,
        "attempt": (existing.get("attempt", 0) + 1) if existing else 1,
    }
    if existing is None:
        # atomic exclusive create prevents a race between two fresh workers
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            other = _read(p)
            print(json.dumps({"ok": False, "reason": "race-lost",
                              "by": (other or {}).get("owner")}))
            return 3
        with os.fdopen(fd, "w") as f:
            json.dump(lease, f, indent=2, sort_keys=True); f.write("\n")
    else:
        _atomic_write(p, lease)  # reclaiming an expired/own lease
    print(json.dumps({"ok": True, "lease": lease}))
    return 0


def renew(issue, owner, ttl):
    p = _path(issue); lease = _read(p)
    if lease is None:
        print(json.dumps({"ok": False, "reason": "not-found"})); return 4
    if lease.get("owner") != owner:
        print(json.dumps({"ok": False, "reason": "owner-mismatch",
                          "by": lease.get("owner")})); return 5
    lease["expires_at"] = _now() + ttl
    lease["renewals"] = int(lease.get("renewals", 0)) + 1
    lease["state"] = "running"
    _atomic_write(p, lease)
    print(json.dumps({"ok": True, "lease": lease})); return 0


def release(issue, owner):
    p = _path(issue); lease = _read(p)
    if lease is None:
        print(json.dumps({"ok": True, "reason": "already-absent"})); return 0
    if lease.get("owner") != owner and not _expired(lease):
        print(json.dumps({"ok": False, "reason": "owner-mismatch",
                          "by": lease.get("owner")})); return 5
    p.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "released": issue})); return 0


def status(issue):
    lease = _read(_path(issue))
    if lease is None:
        print(json.dumps({"held": False})); return 0
    print(json.dumps({"held": not _expired(lease), "expired": _expired(lease),
                      "lease": lease})); return 0


def reap():
    reaped = []
    if LEASE_DIR.exists():
        for p in LEASE_DIR.glob("*.json"):
            lease = _read(p)
            if lease and _expired(lease):
                reaped.append(lease.get("issue", p.stem)); p.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "reaped": reaped})); return 0


def main():
    ap = argparse.ArgumentParser(description="jloop atomic expiring lease manager")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("acquire", "renew"):
        s = sub.add_parser(c); s.add_argument("issue")
        s.add_argument("--owner", required=True); s.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    for c in ("release",):
        s = sub.add_parser(c); s.add_argument("issue"); s.add_argument("--owner", required=True)
    for c in ("status",):
        s = sub.add_parser(c); s.add_argument("issue")
    sub.add_parser("reap")
    a = ap.parse_args()
    if a.cmd == "acquire": sys.exit(acquire(a.issue, a.owner, a.ttl))
    if a.cmd == "renew":   sys.exit(renew(a.issue, a.owner, a.ttl))
    if a.cmd == "release": sys.exit(release(a.issue, a.owner))
    if a.cmd == "status":  sys.exit(status(a.issue))
    if a.cmd == "reap":    sys.exit(reap())


if __name__ == "__main__":
    main()
