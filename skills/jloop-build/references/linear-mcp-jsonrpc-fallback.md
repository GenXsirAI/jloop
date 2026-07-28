# Linear MCP JSON-RPC Fallback

Use when the agent's MCP tool wrapper rejects `mcp__linear__*` calls
(e.g. "Invalid input", "unrecognized keys") but the Linear MCP endpoint is
reachable and the token is valid. This bypasses the tool invoker and speaks
JSON-RPC directly to the MCP server — same schema and arguments as the MCP
tools, so less translation than dropping to raw GraphQL.

## Endpoint and headers

```bash
curl -s -X POST https://mcp.linear.app/mcp \
  -H "Authorization: Bearer $LINEAR_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream"
```

> **SSE wrap:** responses may come back as `event: message` + `data: {...}`
> Server-Sent-Events lines. Parse the JSON payload out of the `data:` line.

## Example — list queue issues

```bash
curl -s -X POST https://mcp.linear.app/mcp \
  -H "Authorization: Bearer $LINEAR_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_issues","arguments":{"label":"approved","assignee":null,"team":"<team name>"}}}' \
  | tail -n 1
```

Extract the text from `result.content[0].text`.

## Teams probe

```bash
... -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_teams","arguments":{}}}'
```

## SSE-stripping helper

```bash
python3 -c "
import sys, json
for line in sys.stdin.read().splitlines():
    if line.startswith('data: '):
        print(json.dumps(json.loads(line[6:]), indent=2)); break
"
```

## When to use

- `mcp__linear__*` tool calls fail with schema/argument errors
- the underlying endpoint is verified reachable (curl works)
- a headless cron pass must continue without interactive debugging

If this path also fails, fall back to `linear-direct-graphql.md`.
