---
name: jloop-watch
description: On-demand trigger for jloop's upstream-drift watchdog (GOL-9). Runs scripts/watch.py to check tracked skills' GitHub sources for advances and file gated spec issues. Use when you want to manually force a drift check instead of waiting for the daily cron.
version: 1.0.0
license: MIT
---

# jloop-watch (on-demand upstream-drift check)

Manual trigger for the watchdog that `scripts/watch.py` implements (spec GOL-9).
The **daily** check is wired as a Hermes cron; this skill is for when you want
to force a check right now (e.g. right after you heard a repo shipped a fix).

## What it does
Runs `python3 scripts/watch.py` from the jloop repo root. For every skill in
`.factory/watch.yaml`, it does a `git ls-remote` (no clone/fetch/apply — see
NG-1) and compares the upstream HEAD + tags against the `last_seen_*` values.
If upstream advanced, it files ONE gated jloop-spec issue per repo
(`spec-waiting-approval`), pinning the exact new SHA. The human adds `approved`,
and the normal jloop-build pipeline performs the upgrade — driving the issue
through `build-in-progress` → `build-complete` (+ `waiting-to-merge`) →
`done`. Nothing is applied automatically. (Terminal label renamed `completed` → `done`; `merged` is a separate signal label added when the PR actually merges.)

## How to run (you are the operator)
```bash
cd ~/jloop
export JLOOP_WORKER_ID="$(whoami)@$(hostname)-$$"
python3 scripts/watch.py            # check + file drift issues
python3 scripts/watch.py --check    # dry run: detect + print payload, no Linear write
python3 scripts/watch.py --backfill # one-time: record provenance for untracked skills
```

## First-time setup (back-fill)
Before the watch can run, `.factory/watch.yaml` must list each skill's source
repo. Run `--backfill` once; it scans `~/.hermes/skills/*` and `~/jloop/skills/*`,
asks for the source repo + ref of any untracked skill, and records it (seeding
the current HEAD as `last_seen_sha`).

## Security
Upstream code is untrusted data. `watch.py` never clones or applies upstream
diffs — detection is `ls-remote` only. The resulting spec issue is a normal
jloop-spec issue gated by the human `approved` label; agents never merge.

## See also
- `scripts/watch.py` — the implementation (single new code path; lease/idempotency/
  verify_scope are PROTECTED and only called, never modified).
- `.factory/watch.yaml` — provenance + last-seen registry (committed state).
- `scripts/idempotency.py` — guarantees at most one drift issue per repo+SHA.
