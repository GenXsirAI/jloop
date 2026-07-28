# Verifying frontend / CSS changes — lint green is not "it works"

When an issue touches UI, lint + typecheck passing is necessary but NOT
sufficient: styling can be completely broken with a green build. Use this
recipe before claiming an AC that has a visual outcome.

## Trap: Tailwind v4 `@theme` silent no-op

A custom token defined under `@theme` (`--color-accent: #2ea043`) whose
utility (`bg-accent`) is never generated produces classes that resolve to
NOTHING — the browser silently falls back. No build/lint/type error.
This is project-wide: every page using the class is affected identically.

## Step 1 — confirm the utility loaded (browser, NOT grep)

Do NOT grep the build output dir for the hex. Modern bundlers (Next +
Turbopack) inline CSS into `<style>` blocks — there may be no `.css` files at
all, so a grep returns 0 hits **even when everything works** (false negative).

Instead, in devtools on BOTH the changed page and an untouched page using the
same class:

```js
getComputedStyle(document.documentElement).getPropertyValue('--color-accent')
// non-empty => tokens loaded; empty => theme CSS never imported
```

To distinguish "bad @theme" from "missing import", compile the entry CSS with
the project's own toolchain from the project dir and check the output for the
token and the utility selector.

## Step 2 — isolate "my code" vs "project-wide"

- Both pages broken → pre-existing project-wide issue: file a separate bug;
  do NOT mark your issue visually verified.
- Only your page broken → your change; fix it.

## Step 3 — dev servers lie about CSS

A long-lived dev server serves stale bundler CSS (symptoms: empty CSS vars on
untouched pages, `net::ERR_ABORTED`, hot node process). Kill it (`-9` if
needed), delete the build cache, restart, re-verify in a fresh browser
session. Never claim visual verification on a dev server older than the
current session.

## Step 4 — end-to-end path check for data-driven UI

If the page fetches from a backend, also confirm the full chain returns 200:

1. Find the exact fetch shape in the page source.
2. The frontend path must match the backend mount point/prefix — an
   off-by-one prefix (`p=labor/...` vs blueprint `/api/labor`) 404s forever.
3. Check any proxy target env var points at the *running* backend port.
4. `curl -s -o /dev/null -w "%{http_code}\n" "<proxied url>"` → must be 200.
5. **Stale backend gotcha:** a backend started before the feature branch
   merged does not have its routes registered. Restart it against current
   default branch, then verify with curl directly against the backend port.

## Decision rule

Step 1 is the objective pass/fail; Step 2 assigns blame; Step 4 applies
whenever the page is data-driven. A `.next`-style grep or a stale dev server
is never evidence.
