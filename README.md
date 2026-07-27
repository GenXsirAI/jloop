# jloop

A hardened, portable **AI software factory** loop: three skills that turn
**Linear + GitHub** into a small, human-gated build pipeline —
**spec → build → review → *humans merge*** — with an **atomic lease** so work
can't be double-claimed, an **idempotency guard** so retries never create
duplicate PRs or comments, and **graph-verified scope enforcement** so an agent
cannot silently exceed the contract.

```
idea → /jloop-spec  (interviews you → files a Linear issue + a machine-readable contract, labels it spec-waiting-approval)
      → you add the `approved` label          ← the one human approval gate
      → /jloop-build (leases the issue → implements only its contract → opens ONE PR → labels it waiting-to-merge + posts a "Solution Ready For Merge" callout on the issue)
      → /jloop-review(graph-verified scope + required CI → posts a verdict + label)
      → you merge                            ← agents never merge
      → /jloop-merge-detect (detects the merge → flips the callout to "Merged / Complete" + clears waiting-to-merge)
```

The Linear state machine is a **closed six-label vocabulary**:
`spec-waiting-approval → approved → build-in-progress → build-complete → waiting-to-merge → done`. Agents set
`spec-waiting-approval` (spec) and `build-complete` + `waiting-to-merge` (PR opened); `approved`
and `done` are human-only gates. `merged` is a separate signal set once the PR is actually merged. On completion, all pipeline labels are
stripped and only `done` remains.

jloop is a from-scratch reimplementation inspired by the ideas in
[finna/Finn-loop](https://github.com/finna/Finn-loop), redesigned to fix its
structural gaps. It ships as plain skills + small Python scripts with **zero
runtime services** and no vendor lock beyond Linear/GitHub.

## Why jloop over a soft-locked loop

| Problem in the simple loop | jloop's fix |
|---|---|
| The Linear assignee "is not an atomic lock" — two sessions can build the same issue | **Atomic expiring lease** (`scripts/lease.py`, `O_CREAT\|O_EXCL` + durable file). A second worker gets exit 3 and picks other work. Crashed workers' leases expire and auto-requeue. |
| Loop retries / races can open duplicate PRs and comments | **Idempotency guard** (`scripts/idempotency.py`): claim an action key *before* the external write; replays become no-ops. One PR per issue, one verdict per commit. |
| Scope compliance is self-reported in prose | **Graph-verified scope** (`scripts/verify_scope.py` + codebase-memory-mcp): changed files are checked against the contract's `relevant_files`/`protected` globs, and `detect_changes` catches *transitive* blast-radius into protected modules. |
| Issue bodies / PR comments are fed to agents as instructions | Every skill treats fetched text as **untrusted DATA**; injection ("ignore non-goals") is a review finding, not a command. |
| One token specs, builds, and reviews | **Least-privilege, separated roles**: build = push-only; review = label+comment, no merge; no role can self-approve. |
| Blocked-by chains not enforced | Builder refuses issues with unresolved blocker relations; `blocked` label ejects work until a human answers. |
| Forced merge discipline | Branches are always `TEAM-NNN-slug` cut from the *fresh* detected default branch; never commit to the default branch; never force-push shared branches; agents never merge or enable auto-merge. |

## Layout

```
skills/jloop-spec/SKILL.md     # interview → Linear issue + .factory contract
skills/jloop-build/SKILL.md    # lease → implement contract → one PR
skills/jloop-review/SKILL.md   # graph-verified scope + CI gates → verdict
skills/jloop-watch/SKILL.md    # GOL-9 upstream-drift watchdog (on-demand)
skills/jloop-merge-detect/SKILL.md  # GOL-12 merged-PR detector (on-demand / cron)
scripts/idempotency.py         # exactly-once external side effects
scripts/verify_scope.py        # contract vs. real diff + graph blast radius
scripts/merge_signal.py        # GOL-10: PR-opened finalize (Solution Ready For Merge callout)
scripts/merge_detect.py        # GOL-12: merged-PR detect (flips callout to Merged / Complete)
scripts/validate.py            # CI + local self-test
.factory/contracts/<ISSUE>.yaml  # machine-readable, enforceable contract
.factory/leases/<ISSUE>.json     # durable claim state (committed)
.factory/actions/<sha1>.json     # durable idempotency records (committed)
```

### Durable state
`.factory/leases` and `.factory/actions` are **committed on purpose** — the claim
and "already done" facts must survive a crashed session and be visible to every
worker and to CI. Only local scratch is gitignored.

## Requirements

- A GitHub repo with a reachable `origin`, and the GitHub CLI (`gh`) authenticated.
- A Linear workspace/team and the Linear connector in your agent.
- An agent that supports skills and a repeat/loop runner.
- **Recommended:** [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)
  indexed on the target repo, so `verify_scope.py` can check transitive scope.
  Set `JLOOP_CBM` (binary path) and `JLOOP_CBM_PROJECT` (from `cbm cli list_projects`).
- At least one **required** GitHub status check for fully automated approvals;
  without required CI, jloop escalates the PR to a human instead of approving.

## Install

1. Copy `skills/jloop-*/SKILL.md` into your agent's skills dir (e.g.
   `.claude/skills/…` or `~/.hermes/skills/…`) and `scripts/` + `.factory/`
   into the target repo.
2. Replace the `TEAM` placeholder in the skills with your Linear team key.
3. Create labels idempotently — Linear: `spec-waiting-approval`, `approved`,
   `waiting-to-merge`, `done`, `merged`, `blocked`; GitHub: `loop-approved`,
   `loop-changes-requested`, `needs-human-review`.
4. Export a worker identity: `export JLOOP_WORKER_ID="$(whoami)@$(hostname)-$$"`.
5. Run `python scripts/validate.py` — it must print `jloop validation OK`.

### Via `npx skills` (recommended)

The toolkit is published as a skill repo, so you can install every `jloop*`
skill in one shot with the Vercel [`skills`](https://skills.sh) CLI (note the
**plural** `skills` — `npx skill add` singular is the wrong, inert package):

```bash
# Generic — installs into the detected agent (Claude Code, Cursor, Hermes, …)
npx skills add GenXsirAI/jloop

# Hermes specifically (non-interactive, skips the universal-agent prompt noise)
npx skills add GenXsirAI/jloop -a hermes-agent -g -y
```

This fetches the repo and copies each `skills/jloop*/SKILL.md` into your agent's
skills directory (`~/.hermes/skills/` for the `-a hermes-agent` form). Scripts
and `.factory/` still need to live in the **target repo** you run jloop against,
so complete steps 2–5 above afterward.

> **Note:** Hermes caches its skill index at session start. A skill installed
> mid-session will not appear in `skills_list()` until you open a **new chat** —
> verify it loaded with `skill_view(name='jloop')`.

## Daily rhythm

1. `/jloop-spec` on any idea → it files the issue and labels it
   `spec-waiting-approval` → read the filed issue + contract → if you approve the
   exact contract, add the `approved` label in Linear (**only a human does this**).
2. Run the builder loop (one builder per team). Optionally run a **separate**
   reviewer session (fresh context, separate identity).
3. Merge only PRs that are `loop-approved`, conflict-free, green on required
   checks, and still at the reviewed SHA. `needs-human-review` means read and
   resolve the escalation first.
4. Run `/jloop-merge-detect` (or let its cron) once a PR is merged — it flips the
   issue's "Solution Ready For Merge" callout to "Merged / Complete" and clears
   the `waiting-to-merge` signal. `done` stays a human-only gate.
5. Answer `blocked` issues, remove the label, and the work requeues.

## The rules that make it work

- If it isn't in the Linear issue **and** the contract, it doesn't exist. No
  side-channel instructions; fetched text is data, never commands.
- One issue per PR, sized to ≤ a day of agent work.
- Acceptance criteria are observable outcomes; non-goals are binding and
  **graph-enforced**. Only editing the issue *and bumping the contract version*
  can change scope.
- Blocked issues and escalated PRs leave the automated queue until a human acts.
- On completion (`Done`/done state), **strip the queue labels**
  `spec-waiting-approval`, `approved`, and `agent-ready`. They are pipeline
  signals (spec drafted / human-approved-to-build) that mean nothing once the
  work is finished; leaving them on makes a done issue look like it is
  still awaiting a decision. A done issue should carry no approval-gate
  label — only a state. (Discovered when GOL-7/GOL-8 kept their gate labels
  after being marked Done.)
- Agents never merge or enable auto-merge. `loop-approved` is evidence, not
  authorization.

## License

MIT — see [LICENSE](LICENSE). Inspired by the design discussion in
[finna/Finn-loop](https://github.com/finna/Finn-loop); all code here is original.
