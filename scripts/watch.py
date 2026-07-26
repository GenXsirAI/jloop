#!/usr/bin/env python3
"""
jloop upstream-drift watchdog (GOL-9).

Records each GitHub-sourced skill's provenance, then watches upstream daily.
Detection is `git ls-remote` ONLY -- we never clone, fetch, or apply upstream
diffs (NG-1). When a tracked upstream advances, we file a gated jloop-spec
issue (labeled spec-waiting-approval) carrying the pinned new_sha; the human
adds `approved`, and the normal jloop-build pipeline performs the upgrade.

Scope (see .factory/contracts/GOL-9.yaml):
  - This script is the only NEW code path.
  - scripts/lease.py, scripts/idempotency.py, scripts/verify_scope.py are
    PROTECTED (NG-3): we call idempotency.py, we never modify it.

Usage:
  watch.py --backfill            # one-time: record provenance for skills not yet tracked
  watch.py                       # run mode: check every tracked repo, file drift issues
  watch.py --check               # run mode without filing any Linear issue (dry run)

State file: .factory/watch.yaml
  skills:
    - name: jloop-spec
      path: /Users/genxsir/.hermes/skills/jloop-spec
      repo: https://github.com/GenXsirAI/jloop
      ref: main                 # the ref we originally pulled from
      last_seen_sha: <40-hex>
      last_seen_tag: v1.2.3     # '' if none seen
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCH_YAML = REPO_ROOT / ".factory" / "watch.yaml"
ACTION_DIR = REPO_ROOT / ".factory" / "actions"
IDEMPOTENCY = REPO_ROOT / "scripts" / "idempotency.py"

HERMES_SKILLS = Path(os.path.expanduser("~/.hermes/skills"))
JLOOP_SKILLS = REPO_ROOT / "skills"

DEFAULT_TEAM = "Gold Medal Equity"
# label the drift issue carries (the human gate is the separate `approved` label)
DRIFT_LABEL = "spec-waiting-approval"
IDEMPOTENCY_KEY_PREFIX = "drift:GOL-9:"

SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------- #
# registry IO (committed state -- readable/writable only here, AC-2)           #
# --------------------------------------------------------------------------- #
def load_registry():
    if not WATCH_YAML.exists():
        return {"skills": []}
    try:
        import yaml

        data = yaml.safe_load(WATCH_YAML.read_text()) or {}
        data = data if isinstance(data, dict) else {"skills": []}
    except Exception:
        # minimal fallback: never crash the watcher on a malformed file
        return {"skills": []}
    # Git SHAs are hex and may be all-digit; PyYAML can parse them as ints (e.g.
    # 40 zeros -> 0), which would corrupt the value and break drift detection.
    # A *real* persisted SHA is always a 40-char hex string. If we see an int
    # (or anything not a 40-char hex string) it was YAML-mangled, so reset it to
    # "" -- the next run re-resolves the real HEAD from the repo.
    for s in data.get("skills", []):
        for key in ("last_seen_sha", "last_seen_tag", "ref", "repo", "path", "name"):
            if key not in s or s[key] is None:
                if key == "last_seen_sha":
                    s[key] = ""
                continue
            v = s[key]
            if key == "last_seen_sha" and (not isinstance(v, str) or not SHA_RE.match(v)):
                s[key] = ""  # mangled/corrupt sha -> re-resolve on next run
            elif not isinstance(v, str):
                s[key] = str(v)
    return data


def save_registry(data):
    WATCH_YAML.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml

        # A 40-char all-hex (or all-digit) SHA is valid YAML as an *integer*
        # (e.g. 40 ones parses as int 111...111), which would corrupt the value
        # on reload. Force-quote any string that is all-digits or a 40-hex SHA so
        # it round-trips as a string.
        class _QuotedDumper(yaml.SafeDumper):
            pass

        def _repr_str(dumper, value):
            if re.fullmatch(r"[0-9]+", value) or SHA_RE.match(value):
                # style='' -> single-quoted, preventing numeric interpretation
                return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="'")
            return dumper.represent_scalar("tag:yaml.org,2002:str", value)

        _QuotedDumper.add_representer(str, _repr_str)
        text = yaml.dump(data, Dumper=_QuotedDumper, sort_keys=False,
                         default_flow_style=False)
    except Exception:
        text = json.dumps(data, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(WATCH_YAML.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, WATCH_YAML)


def find_skill(name):
    for base in (HERMES_SKILLS, JLOOP_SKILLS):
        cand = base / name
        if cand.is_dir():
            return str(cand)
    return None


# --------------------------------------------------------------------------- #
# detection (git ls-remote only -- NG-1: no clone/fetch/apply)                 #
# --------------------------------------------------------------------------- #
def ls_remote(repo):
    """Return (default_branch, head_sha, tags_dict) or None on failure."""
    try:
        r = subprocess.run(
            ["git", "ls-remote", repo],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return None, None, None, f"ls-remote failed: {e}"
    if r.returncode != 0:
        return None, None, None, (r.stderr.strip() or "ls-remote non-zero")
    head_sha = None
    default_branch = "main"
    tags = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref == "HEAD":
            continue
        if ref.startswith("refs/heads/"):
            branch = ref[len("refs/heads/"):]
            # crude default-branch heuristic: main > master > first branch
            if branch in ("main", "master"):
                default_branch = branch
                head_sha = sha
            elif head_sha is None:
                head_sha = sha
        elif ref.startswith("refs/tags/"):
            t = ref[len("refs/tags/"):]
            # deref annotated tags (^{}) -- keep the commit sha
            if t.endswith("^{}"):
                t = t[:-3]
            tags[t] = sha
    return default_branch, head_sha, tags, None


def newest_semver(tags):
    vers = [t for t in tags if SEMVER_RE.match(t)]
    if not vers:
        return ""
    def key(t):
        nums = SEMVER_RE.match(t).group(0).lstrip("v").split(".")
        return tuple(int(x) for x in nums)
    return max(vers, key=key)


def _semver_nums(t):
    if not t:
        return None
    m = SEMVER_RE.match(t)
    if not m:
        return None
    try:
        return tuple(int(x) for x in m.group(0).lstrip("v").split("."))
    except Exception:
        return None


def semver_gt(a, b):
    if not a:
        return False
    if not b:
        return True
    ka, kb = _semver_nums(a), _semver_nums(b)
    if ka is None or kb is None:
        return False
    return ka > kb


# --------------------------------------------------------------------------- #
# Linear filing (idempotency-guarded: one issue per repo+sha)                  #
# --------------------------------------------------------------------------- #
def linear_filed(repo, new_sha):
    """Return True if a drift issue for this repo+sha was already claimed."""
    key = f"{IDEMPOTENCY_KEY_PREFIX}{repo}:{new_sha}"
    if not IDEMPOTENCY.exists():
        return False
    r = subprocess.run(
        [sys.executable, str(IDEMPOTENCY), "claim", key],
        capture_output=True, text=True,
    )
    try:
        out = json.loads(r.stdout)
    except Exception:
        out = {}
    # claim exit 0 -> we own it (file now); exit 3 -> already claimed
    if r.returncode == 3:
        return True
    if r.returncode == 0:
        # mark committed so a third run still sees it as done
        subprocess.run(
            [sys.executable, str(IDEMPOTENCY), "commit", key,
             "--meta", json.dumps({"repo": repo, "sha": new_sha})],
            capture_output=True, text=True,
        )
        return False
    # unexpected: be safe, don't spam (treat as already filed)
    return True


def _rel_or_name(p, root):
    try:
        return p.relative_to(root)
    except Exception:
        return p.name


def file_drift_issue(repo, name, new_sha, old_sha, new_tag, old_tag, note=""):
    """File a gated jloop-spec issue for this repo+sha. No-op if already filed."""
    if linear_filed(repo, new_sha):
        return None  # idempotent: never duplicate
    watch_rel = _rel_or_name(WATCH_YAML, REPO_ROOT)
    body = f"""## Problem
The upstream source of a tracked skill has advanced since last seen. This is an
auto-filed **upstream-drift** proposal (GOL-9 watchdog). It is a normal
jloop-spec issue: review the diff, then add `approved` to authorize the upgrade.

## Acceptance Criteria
- [ ] **AC-1** — Pull upstream commits for `{name}` up to the pinned SHA into its skill directory, preserving local customizations (merge/rebase, not overwrite).
- [ ] **AC-2** — Verify the skill still loads (SKILL.md parses, no broken scripts) after the pull.
- [ ] **AC-3** — Update `{watch_rel}` `last_seen_sha`/`last_seen_tag` for this skill to the pinned SHA.

## Non-goals
- NG-1 — Do not apply unpinned "latest" upstream; the SHA below is the exact target.
- NG-2 — No auto-merge; human merges after review.

## Relevant files
- `{name}` skill directory (path in registry).

## How to verify
1. Diff the pinned SHA vs current local copy.
2. Pull only that SHA; confirm local edits survive.
3. Skill loads; registry updated.

**Pinned upgrade target (exact SHA):** `{new_sha}`
**Previous seen SHA:** `{old_sha or 'unknown'}`
**Tag delta:** `{old_tag or 'none'}` -> `{new_tag or 'none'}`
{note}
"""
    # File via Linear MCP (the agent's connector). Build the minimal payload and
    # hand it to the MCP through the agent by printing JSON the caller can use.
    # Here we emit a structured instruction the agent executes with the connector.
    payload = {
        "action": "create_issue",
        "team": DEFAULT_TEAM,
        "title": f"[drift] {name}: upstream advanced -> {new_sha[:12]}",
        "labels": ["Feature", DRIFT_LABEL],
        "body": body,
        "idempotency_key": f"{IDEMPOTENCY_KEY_PREFIX}{repo}:{new_sha}",
    }
    # Persist the intent so the agent (which has the Linear connector) can file it.
    ACTION_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ACTION_DIR), suffix=".drift.json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2); f.write("\n")
    # Signal to stdout for the agent to act on.
    print(json.dumps({"drift_detected": True, "file": tmp, "payload": payload},
                     indent=2), file=sys.stderr)
    return payload


# --------------------------------------------------------------------------- #
# backfill                                                                     #
# --------------------------------------------------------------------------- #
def cmd_backfill(ask_fn):
    data = load_registry()
    tracked = {s.get("name") for s in data.get("skills", [])}
    found = []
    for base in (HERMES_SKILLS, JLOOP_SKILLS):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                found.append(d.name)
    untracked = [n for n in found if n not in tracked]
    if not untracked:
        print("backfill: all", len(found), "skills already tracked. nothing to do.")
        return
    print(f"backfill: {len(untracked)} untracked skill(s): {', '.join(untracked)}")
    for name in untracked:
        path = find_skill(name)
        # heuristic default: any github URL embedded in the SKILL.md
        default_repo = ""
        skill_md = Path(path) / "SKILL.md" if path else None
        if skill_md is not None and skill_md.exists():
            m = re.search(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                          skill_md.read_text(errors="ignore"))
            if m:
                default_repo = m.group(0)
        ans = ask_fn(name, path, default_repo)
        if not ans:
            print(f"  skip {name} (no source provided)")
            continue
        repo, ref = ans
        # resolve current HEAD/tag to seed last_seen_*
        _, head_sha, tags, err = ls_remote(repo)
        if not head_sha:
            # repo unreachable / not found: do NOT record a dead entry (it would
            # never resolve and would silently report "no drift" forever). Leave
            # it untracked so back-fill re-prompts on the next run.
            print(f"  SKIP {name}: cannot resolve repo '{repo}' ({err or 'no HEAD'}). Fix the URL and re-run --backfill.")
            continue
        last_tag = newest_semver(tags) if tags else ""
        entry = {
            "name": name, "path": path, "repo": repo, "ref": ref or "main",
            "last_seen_sha": head_sha or "", "last_seen_tag": last_tag,
        }
        data.setdefault("skills", []).append(entry)
        save_registry(data)
        print(f"  recorded {name}: repo={repo} ref={ref or 'main'} sha={head_sha or '?'} tag={last_tag or '-'}")
    print("backfill complete. registry:", WATCH_YAML)


# --------------------------------------------------------------------------- #
# run mode                                                                     #
# --------------------------------------------------------------------------- #
def cmd_run(dry_run=False):
    data = load_registry()
    skills = data.get("skills", [])
    if not skills:
        print("watch: no tracked skills. run --backfill first.")
        return
    total_drift = 0
    for s in skills:
        name = s.get("name", "?")
        repo = s.get("repo", "")
        if not repo:
            print(f"watch: {name}: no repo recorded, skipping.")
            continue
        default_branch, head_sha, tags, err = ls_remote(repo)
        if err:
            print(f"watch: {name}: ERROR {err} -- skipping.")
            continue
        old_sha = s.get("last_seen_sha", "")
        old_tag = s.get("last_seen_tag", "") or ""
        new_tag = newest_semver(tags) if tags else ""
        drifted = (head_sha and old_sha and head_sha != old_sha) or semver_gt(new_tag, old_tag)
        if not drifted:
            print(f"watch: {name}: no drift (sha={head_sha[:12] if head_sha else '?'}, tag={old_tag or '-'})")
            continue
        # drift detected
        total_drift += 1
        commit_count_est = ""  # ls-remote cannot count without fetch; placeholder
        payload = {
            "repo": repo, "old_sha": old_sha, "new_sha": head_sha,
            "tag_delta": f"{old_tag or 'none'} -> {new_tag or 'none'}",
            "commit_count_estimate": commit_count_est,
        }
        print(f"watch: {name}: DRIFT {payload['tag_delta']} head={head_sha[:12] if head_sha else '?'}")
        print("  payload: " + json.dumps(payload))
        # advance last_seen_* in the registry (the durable "seen" fact)
        s["last_seen_sha"] = head_sha or s.get("last_seen_sha", "")
        s["last_seen_tag"] = new_tag
        save_registry(data)
        if dry_run:
            print("  (dry-run: not filing Linear issue)")
            continue
        # File gated issue (idempotent per repo+sha)
        file_drift_issue(repo, name, head_sha or "", old_sha, new_tag, old_tag)
    print(f"watch: done. {total_drift} drift(s) detected.")


def main():
    ap = argparse.ArgumentParser(description="jloop upstream-drift watchdog (GOL-9)")
    ap.add_argument("--backfill", action="store_true", help="record provenance for untracked skills (asks per skill)")
    ap.add_argument("--check", action="store_true", help="run without filing any Linear issue (dry run)")
    a = ap.parse_args()

    if a.backfill:
        # ask_fn interacts with the user per skill; default text offered.
        def ask(name, path, default_repo):
            print(f"\nSkill '{name}' ({path})")
            print(f"  suggested source repo: {default_repo or '(none found in SKILL.md)'}")
            return _prompt(name, default_repo)
        cmd_backfill(ask)
        return
    cmd_run(dry_run=a.check)


def _prompt(name, default_repo):
    """Interactive prompt; falls back to non-interactive skip if no tty."""
    try:
        if not sys.stdin.isatty():
            return None
        repo = input(f"  source repo URL for '{name}' [{default_repo}]: ").strip()
        repo = repo or default_repo
        if not repo:
            return None
        ref = input(f"  ref (branch/tag) you pulled from [main]: ").strip() or "main"
        return (repo, ref)
    except (EOFError, KeyboardInterrupt):
        return None


if __name__ == "__main__":
    main()
