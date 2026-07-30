#!/usr/bin/env bash
#
# jloop merge-label cron — closes the gap where merged PRs leave their
# Linear issues stuck in In Progress / waiting-to-merge.
#
# For every Gold Medal Equity issue carrying the `waiting-to-merge` label:
#   - resolve its linked PR (gh search for "GOL-NN in:body")
#   - if the PR is MERGED on GitHub, apply via Linear GraphQL:
#       * add labels `done` + `merged`, remove `waiting-to-merge`
#       * set issue state to Done
#
# SAFETY: default mode is DRY-RUN (scans + prints intended actions, no writes).
# Pass --apply to actually mutate Linear. The cron registration uses --apply.
#
# Idempotent: once an issue is `done`, it is skipped.
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
        out = subprocess.run(['gh','pr','list','--search', ident+' in:body',
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
        cur = [l['id'] for l in lab_nodes]
        if L_WAITING in cur: cur.remove(L_WAITING)
        if L_DONE not in cur: cur.append(L_DONE)
        if L_MERGED not in cur: cur.append(L_MERGED)
        q = '''mutation($id:String!,$labels:[String!],$state:String!){
          issueUpdate(id:$id, input:{ labelIds:$labels, stateId:$state }){ success }
        }'''
        if APPLY:
            res = lgql(q, {'id': iid, 'labels': cur, 'state': S_DONE})
            print('    APPLIED:', res.get('data', {}).get('issueUpdate'), res.get('errors', ''))
        else:
            print('    (dry-run) would set labels=%s state=Done' % cur)
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
print('  merged_fixed=%d stuck_closed_unmerged=%d unrecoverable_errors=%d'
      % (merged_fixed, len(stuck_closed), unrecoverable))
if stuck_closed:
    ids = ', '.join('%s(#%s)' % (i, n) for i, n in stuck_closed)
    print('  NEEDS ATTENTION: issues still carrying waiting-to-merge but whose PR was '
          'closed without merge -> %s. Remove the waiting-to-merge label (PR abandoned).' % ids)
if unrecoverable:
    # Surface as a hard failure so the cron run is marked error and delivered.
    print('  UNRECOVERABLE: %d issue(s) could not be processed (network/API).' % unrecoverable)
    sys.exit(1)
PY
log "done."
