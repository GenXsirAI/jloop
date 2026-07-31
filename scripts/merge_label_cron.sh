#!/usr/bin/env bash
#
# jloop merge-label cron — closes the gap where merged PRs leave their
# Linear issues stuck in In Progress / waiting-to-merge / "Solution Ready For Merge".
#
# Pass 1 — every issue carrying `waiting-to-merge`:
#   - resolve its linked PR (gh search for "GOL-NN in:body", --state all)
#   - if the PR is MERGED on GitHub, apply via Linear GraphQL:
#       * strip every state label, add `done` + `merged`
#       * set issue state to Done
#       * refresh the description callout to "Merged / Complete"
#   - if the PR is CLOSED (not merged), flag as STUCK (waiting-to-merge is stale)
#
# Pass 2 — every issue already `done`/`merged` whose description STILL shows
#   "Solution Ready For Merge": refresh the callout to "Merged / Complete".
#   (Catches the case where the label flip happened but the description update
#   was missed — the exact GOL-20/21/27 drift that recurred.)
#
# SAFETY: default mode is DRY-RUN (scans + prints intended actions, no writes).
# Pass --apply to actually mutate Linear. The cron registration uses --apply.
#
# Idempotent: pass 1 skips issues already `done`; pass 2 is a no-op when the
# callout already reads "Merged / Complete".
# Secrets read from Bitwarden (`bws`) — never inlined. Requires bws, gh, python3.
#
set -euo pipefail

APPLY=0
for a in "$@"; do [ "$a" = "--apply" ] && APPLY=1; done

BWS="${HOME}/.hermes/bin/bws"
PROJECT_ID="c122544c-deb6-4e20-b7c5-b4930041be18"
TEAM_ID="88580444-fb79-4775-aa5c-a6eac1da51bd"

L_DONE="8d2d882f-4ffe-4251-9c0b-6b6d98d2dd89"
L_MERGED="8debe854-8c4a-4c15-aaa6-3c2e9144284b"
L_WAITING="6a18d96c-0fc3-4816-8026-e04592b26ab2"
S_DONE="8351117e-1781-4972-879c-cf9085724896"

export PATH="$HOME/.hermes/bin:$HOME/bin:$PATH"
CRON_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$CRON_DIR")"
export REPO_ROOT
log() { echo "[merge-label-cron $(date -u +%FT%TZ)] $*"; }
[ "$APPLY" -eq 1 ] && log "MODE: APPLY (mutations enabled)" || log "MODE: DRY-RUN (no mutations)"

LIN_ID=$( "$BWS" secret list "$PROJECT_ID" 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);items=d if isinstance(d,list) else d.get('data',d);print(next(i['id'] for i in items if i.get('key')=='LINEAR_API_KEY'))" )
LINEAR_API_KEY=$( "$BWS" secret get "$LIN_ID" 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('value') or d.get('data',{}).get('value',''))" )
GH_ID=$( "$BWS" secret list "$PROJECT_ID" 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);items=d if isinstance(d,list) else d.get('data',d);print(next(i['id'] for i in items if i.get('key')=='GITHUB_TOKEN'))" )
GITHUB_TOKEN=$( "$BWS" secret get "$GH_ID" 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('value') or d.get('data',{}).get('value',''))" )
[ -n "${LINEAR_API_KEY:-}" ] || { log "ERROR: LINEAR_API_KEY unavailable"; exit 1; }
[ -n "${GITHUB_TOKEN:-}" ] || { log "ERROR: GITHUB_TOKEN unavailable"; exit 1; }

lgql() { python3 -c '
import sys, json, urllib.request
tok = sys.argv[1]; payload = json.loads(sys.argv[2])
req = urllib.request.Request("https://api.linear.app/graphql",
    data=json.dumps(payload).encode(),
    headers={"Authorization": tok, "Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
' "$LINEAR_API_KEY" "$1"; }

log "scanning issues with waiting-to-merge label..."
SCAN='{"query":"query($tid:String!,$lname:String!){ team(id:$tid){ labels(filter:{name:{eq:$lname}}){ nodes{ id name issues(first:50){ nodes{ id identifier state{ id name type } } } } } } }","variables":{"tid":"'"$TEAM_ID"'","lname":"waiting-to-merge"}}'
ISSUES=$( lgql "$SCAN" )

printf '%s' "$ISSUES" | python3 -c "import sys,json;d=json.load(sys.stdin);nodes=d['data']['team']['labels']['nodes'];iss=[i for l in nodes for i in l['issues']['nodes']];print('  found',len(iss),'candidate(s)')"

ISSUES_JSON="$ISSUES" python3 - "$LINEAR_API_KEY" "$GITHUB_TOKEN" "$L_DONE" "$L_MERGED" "$L_WAITING" "$S_DONE" "$APPLY" <<'PY'
import sys, json, subprocess, os
tok = sys.argv[1]; ght = sys.argv[2]
L_DONE, L_MERGED, L_WAITING, S_DONE, APPLY = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], int(sys.argv[7])
# Closed state-label vocabulary (must match scripts/merge_signal.py STATE_LABELS).
STATE_LABELS_NAMES = ['spec-waiting-approval', 'approved', 'build-in-progress',
                      'build-complete', 'waiting-to-merge', 'done']
SCRIPT_DIR = __import__('pathlib').Path(os.environ.get('REPO_ROOT', os.getcwd()))
nodes = json.loads(os.environ['ISSUES_JSON'])['data']['team']['labels']['nodes']
issues = [i for l in nodes for i in l['issues']['nodes']]

def lgql(q, v):
    import urllib.request
    req = urllib.request.Request('https://api.linear.app/graphql',
        data=json.dumps({'query': q, 'variables': v}).encode(),
        headers={'Authorization': tok, 'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))

LABELS_Q = 'query($iid:String!){ issue(id:$iid){ labels{ nodes{ id name } } } }'

unrecoverable = 0
merged_fixed = 0
callout_fixed = 0
stuck_closed = []   # (ident, pr_number) waiting-to-merge but PR closed-unmerged
errs = []

for it in issues:
    ident, iid = it['identifier'], it['id']
    # 'done' check requires fetching labels now (scan omitted them for complexity)
    try:
        lab_nodes = lgql(LABELS_Q, {'iid': iid})['data']['issue']['labels']['nodes']
    except Exception as e:
        print('  WARN', ident, 'label fetch failed:', e); unrecoverable += 1; continue
    labels = [l['name'] for l in lab_nodes]
    if 'done' in labels:
        print('  skip', ident, '(already done)'); continue
    try:
        out = subprocess.run(['gh','pr','list','--state','all','--search', ident+' in:body',
                              '--json','number,state,url'],
                             capture_output=True, text=True, timeout=30).stdout
        prs = json.loads(out) if out.strip() else []
    except Exception as e:
        print('  WARN', ident, 'gh search failed:', e); unrecoverable += 1; continue
    merged = [p for p in prs if p.get('state') == 'MERGED']
    closed = [p for p in prs if p.get('state') == 'CLOSED']
    if merged:
        pr = merged[0]
        print('  DETECT', ident, '-> PR #%s MERGED' % pr['number'])
        # Strip EVERY state label (waiting-to-merge/approved/build-complete/...),
        # keep only non-state labels (Feature/Improvement/Bug), add done+merged.
        keep = [l['id'] for l in lab_nodes if l['name'] not in globals()['STATE_LABELS_NAMES']]
        if L_DONE not in keep: keep.append(L_DONE)
        if L_MERGED not in keep: keep.append(L_MERGED)
        # Refresh the description callout to 'Merged / Complete' so it no longer
        # reads 'Solution Ready For Merge' after merge.
        new_desc = None
        if APPLY:
            try:
                cur_desc = lgql('''query($iid:String!){ issue(id:$iid){ description } }''',
                                {'iid': iid})['data']['issue']['description'] or ''
                td = subprocess.run([sys.executable, SCRIPT_DIR/'scripts'/'merge_signal.py',
                                     'transform-description', '--url', pr['url'], '--merged'],
                                    input=cur_desc, capture_output=True, text=True, timeout=30)
                if td.returncode == 0:
                    new_desc = td.stdout
            except Exception as e:
                print('    WARN', ident, 'desc transform failed:', e)
        q = '''mutation($id:String!,$labels:[String!],$state:String!,$desc:String){
          issueUpdate(id:$id, input:{ labelIds:$labels, stateId:$state, description:$desc }){ success }
        }'''
        if APPLY:
            res = lgql(q, {'id': iid, 'labels': keep, 'state': S_DONE,
                           'desc': new_desc if new_desc else (lgql('''query($iid:String!){ issue(id:$iid){ description } }''', {'iid': iid})['data']['issue']['description'] or '')})
            print('    APPLIED:', res.get('data', {}).get('issueUpdate'), res.get('errors', ''))
        else:
            print('    (dry-run) would strip all state labels + set done/merged + refresh callout to Merged / Complete')
        merged_fixed += 1
        continue
    if closed:
        # B (visibility): waiting-to-merge but PR was closed WITHOUT merge.
        # The issue is stuck — flag it so a human removes waiting-to-merge.
        pr = closed[0]
        print('  STUCK', ident, '-> PR #%s CLOSED (not merged); waiting-to-merge is stale' % pr['number'])
        stuck_closed.append((ident, pr['number']))
        continue
    print('  skip', ident, '(no merged/closed PR; states=%s)' % [p.get('state') for p in prs])

# ---- self-healing summary (recommendation A) ----
print('--- summary ---')
print('  merged_fixed=%d stuck_closed_unmerged=%d callout_fixed=%d unrecoverable_errors=%d'
      % (merged_fixed, len(stuck_closed), callout_fixed, unrecoverable))
if stuck_closed:
    ids = ', '.join('%s(#%s)' % (i, n) for i, n in stuck_closed)
    print('  NEEDS ATTENTION: issues still carrying waiting-to-merge but whose PR was '
          'closed without merge -> %s. Remove the waiting-to-merge label (PR abandoned).' % ids)
if unrecoverable:
    # Surface as a hard failure so the cron run is marked error and delivered.
    print('  UNRECOVERABLE: %d issue(s) could not be processed (network/API).' % unrecoverable)
    sys.exit(1)
PY

# ===========================================================================
# PASS 2 — done/merged issues whose description STILL says "Solution Ready For
# Merge" (label flip happened but the callout refresh was missed — the exact
# GOL-20/21/27 drift). Refresh the callout to "Merged / Complete".
# ===========================================================================
log "scanning done issues for stale 'Solution Ready For Merge' callouts..."
SCAN_DONE='{"query":"query($tid:String!,$lname:String!){ team(id:$tid){ labels(filter:{name:{eq:$lname}}){ nodes{ id name issues(first:50){ nodes{ id identifier description } } } } } }","variables":{"tid":"'"$TEAM_ID"'","lname":"done"}}'
ISSUES_DONE=$( lgql "$SCAN_DONE" )
printf '%s' "$ISSUES_DONE" | python3 -c "import sys,json;d=json.load(sys.stdin);nodes=d['data']['team']['labels']['nodes'];iss=[i for l in nodes for i in l['issues']['nodes']];print('  found',len(iss),'done candidate(s)')"

ISSUES_DONE_JSON="$ISSUES_DONE" python3 - "$LINEAR_API_KEY" "$L_DONE" "$L_MERGED" "$APPLY" <<'PY2'
import sys, json, subprocess, os, re
tok = sys.argv[1]; L_DONE, L_MERGED, APPLY = sys.argv[2], sys.argv[3], int(sys.argv[4])
SCRIPT_DIR = __import__('pathlib').Path(os.environ.get('REPO_ROOT', os.getcwd()))
CALLOUT_RE = re.compile(r"\*\*✅ Solution Ready For Merge\*\*\s*\n\[[^\]]*\]\([^)]*\)")
nodes = json.loads(os.environ['ISSUES_DONE_JSON'])['data']['team']['labels']['nodes']
issues = [i for l in nodes for i in l['issues']['nodes']]

def lgql(q, v):
    import urllib.request
    req = urllib.request.Request('https://api.linear.app/graphql',
        data=json.dumps({'query': q, 'variables': v}).encode(),
        headers={'Authorization': tok, 'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))

callout_fixed = 0
for it in issues:
    ident, iid = it['identifier'], it['id']
    desc = it.get('description') or ''
    if not CALLOUT_RE.search(desc):
        continue  # callout already correct (or absent) -> skip
    print('  STALE CALLOUT', ident, '-> refresh to Merged / Complete')
    # find the PR url from the callout link
    m = re.search(r'\[[^\]]*\]\(([^)]+)\)', desc)
    url = m.group(1).strip().rstrip('>').strip('<>') if m else ''
    if not url:
        print('    WARN', ident, 'no PR url in callout; skipping'); continue
    if not APPLY:
        print('    (dry-run) would set callout to Merged / Complete for', url); callout_fixed += 1; continue
    td = subprocess.run([sys.executable, SCRIPT_DIR/'scripts'/'merge_signal.py',
                         'transform-description', '--url', url, '--merged'],
                        input=desc, capture_output=True, text=True, timeout=30)
    if td.returncode != 0:
        print('    WARN', ident, 'transform failed:', td.stderr[:200]); continue
    q = '''mutation($iid:String!,$d:String!){ issueUpdate(id:$iid, input:{ description:$d }){ success } }'''
    res = lgql(q, {'iid': iid, 'd': td.stdout})
    print('    APPLIED:', res.get('data', {}).get('issueUpdate'), res.get('errors', ''))
    callout_fixed += 1

print('--- pass2 summary ---')
print('  callout_fixed=%d' % callout_fixed)
PY2

# ===========================================================================
# PASS 3 — board-hygiene audit (GOL-28). Catches state/label contradictions
# the merge reconciliation misses: Duplicate/Canceled still carrying build
# labels (R1), done/merged labels on a non-Done state (R2), pipeline labels
# lingering on done/merged issues (R3). Advisory findings (R4/R5) are reported
# only. Applies the same --apply policy as the rest of this cron.
# ===========================================================================
log "running board-hygiene audit..."
REPO_ROOT="$(dirname "$CRON_DIR")"
python3 "${REPO_ROOT}/scripts/board_audit.py" $( [ "$APPLY" -eq 1 ] && echo "--apply" ) 2>&1 | sed 's/^/  [audit] /'

log "done."
