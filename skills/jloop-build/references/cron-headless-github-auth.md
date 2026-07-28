# GitHub `gh` auth in cron / headless / no-tty environments

## Symptom

`gh auth status` reports an invalid token even though the credential store
(e.g. macOS Keychain via `git credential fill`) holds a working token and raw
API calls with it succeed:

```
X Failed to log in to github.com account <user> (default)
 - The token in default is invalid.
 - To re-authenticate, run: gh auth login -h github.com
```

`~/.config/gh/hosts.yml` shows the user configured but the token blob is
missing or stale. In an interactive terminal the keychain prompt can surface
the credential; in cron/launchd/headless there is no prompt, so `gh` never
refreshes it.

## Fix

In non-interactive scheduled passes:

1. Run `gh auth status` **only as a probe**. If invalid, do NOT run
   `gh auth login` (requires a TTY).
2. Fall back to the REST API with a token read directly from the credential
   store:
   ```bash
   TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | sed -n 's/^password=//p')
   curl -s -H "Authorization: Bearer $TOKEN" https://api.github.com/user
   ```
   or export `GH_TOKEN="$TOKEN"` so `gh` itself uses it for this process.
3. If neither works, abort the pass with a clear auth-blocker message so the
   user can fix `gh` interactively.

## Why this is step 0

Every build pass must verify auth BEFORE claiming a Linear issue. Detecting a
dead token here prevents half-finished passes where `gh pr list` /
`gh pr create` fail after the lease was already taken and Linear state
mutated.
