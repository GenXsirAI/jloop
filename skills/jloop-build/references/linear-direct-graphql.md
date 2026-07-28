# Linear Direct GraphQL Fallback

Use when the MCP `mcp__linear__*` tooling is unavailable, returns schema
validation errors, or produces an empty result while issues clearly exist.
The loop degrades: MCP tools → this direct GraphQL path → stop and report.

## Auth

Endpoint `https://api.linear.app/graphql`. Both `Authorization: <raw key>`
and `Authorization: Bearer <key>` work for the **GraphQL API** (note: some
tooling paths accept ONLY the raw-key form — when in doubt, use raw). Test a
candidate key with a trivial probe:

```bash
curl -sS "https://api.linear.app/graphql" \
  -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"{ teams(first:1){ nodes { id key } } }"}'
```

## Teams probe

```graphql
{ teams(first: 50) { nodes { id name key } } }
```

Match `key` against `team_key` in `.factory/jloop.yaml` to get the team UUID.

## Listing queue issues — known schema constraints

- `IssueFilter` has no `label` field; use `labels`, which is an
  `IssueLabelCollectionFilter` — match with `some` / `every` / `length`,
  not `contains` at the top level.
- Unassigned filter: `assignee: { null: true }`. There is no `assigneeId`
  filter field.
- `team.issues` `orderBy` accepts only `PaginationOrderBy` (`createdAt`,
  `updatedAt`). Sorting by `priority`/`number` triggers a validation error —
  sort client-side.

### Working query (approved, unassigned, not blocked)

```graphql
{
  teams(first: 20) {
    nodes {
      key
      issues(
        first: 50
        filter: {
          labels: { some: { name: { eq: "approved" } } }
          assignee: { null: true }
          state: { name: { nin: ["Done", "Canceled"] } }
        }
        orderBy: createdAt
      ) {
        nodes {
          id identifier title priority
          state { name }
          labels { nodes { name } }
        }
      }
    }
  }
}
```

Filter out `blocked`-labeled issues client-side (collection filters can't
express "does NOT have label X" cleanly).

## Workflow states

```graphql
{ teams(first: 10) { nodes { key states(first: 20) { nodes { id name type position } } } } }
```

State transitions need the state **UUID**, not the display name.

## Mutations

```graphql
mutation UpdateIssue($id: ID!, $assigneeId: String, $stateId: String) {
  issueUpdate(id: $id, input: { assigneeId: $assigneeId, stateId: $stateId }) {
    success
    issue { id state { name } assignee { name } }
  }
}
```

Issue creation:

```graphql
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier title url state { name } labels { nodes { name } } }
  }
}
```

`input` takes `teamId`, optional `projectId`, `title`, `description`
(markdown), `stateId`, `labelIds` (UUIDs — get them from `team.labels`; create
missing pipeline labels with `issueLabelCreate(input:{name, teamId, color})`).

## One-shot helper

```bash
python3 - <<'PY'
import json, os, urllib.request
KEY = os.environ["LINEAR_API_KEY"]
QUERY = "..."  # one of the queries above verbatim
req = urllib.request.Request(
    "https://api.linear.app/graphql",
    data=json.dumps({"query": QUERY}).encode(),
    headers={"Authorization": KEY, "Content-Type": "application/json"},
    method="POST")
with urllib.request.urlopen(req, timeout=30) as r:
    print(json.dumps(json.loads(r.read()), indent=2))
PY
```
