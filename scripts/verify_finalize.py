#!/usr/bin/env python3
"""
jloop finalize verifier — prove the issue actually shows what shipped.

Why this exists
---------------
Step 7 of jloop-build finalizes an issue with merge_signal.py, which emits ONE
payload with two halves: a label swap AND a new_description carrying the
"Solution Ready For Merge" callout with the PR link. Applying only the label
half leaves the issue looking finished while showing no trace of what shipped.

That is not hypothetical. It has now happened twice:
  - session 1efa6b (labels swapped, description never updated)
  - session GI-28  (labels set BY HAND, merge_signal.py never run at all --
                    .factory/idempotency/ was empty, proving no finalize ran)

The skill already warned about it in prose both times. Prose was not enough: an
agent can read "verify the callout is present" and still skip it, because
nothing fails when it does. So this is a script -- run it before releasing the
lease and a missing callout becomes a non-zero exit instead of a silent gap.

The general lesson, and the reason this file exists at all: confirming that a
mutation CALL was made is not the same as confirming the RESULT. Read it back.

Usage:
  verify_finalize.py TEAM-123 [--url <pr_url>] [--expect-labels a,b]

Exit codes: 0 all good; 2 bad usage / missing token; 6 API error;
7 finalize incomplete (callout and/or PR link missing).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.linear.app/graphql"
TOKEN_VARS = ("LINEAR_API_KEY", "MCP_LINEAR_API_KEY")

# Accept either the pre-merge callout emitted by merge_signal.py or a
# completed-state variant, so this works before and after the merge lands.
CALLOUT_MARKERS = ("Solution Ready For Merge", "Shipped & Merged")

QUERY = """
query($id:String!){
  issue(id:$id){
    identifier
    description
    state{ name }
    labels{ nodes{ name } }
    attachments{ nodes{ url } }
  }
}
"""


def token():
    for var in TOKEN_VARS:
        if os.environ.get(var):
            return os.environ[var]
    return None


def fetch(issue, key) -> dict:
    payload = json.dumps({"query": QUERY, "variables": {"id": issue}}).encode()
    req = urllib.request.Request(
        API,
        data=payload,
        headers={"Authorization": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": f"HTTP {e.code}: {e.read().decode()[:300]}"}]}
    except Exception as e:
        return {"errors": [{"message": f"{type(e).__name__}: {e}"}]}


def main():
    ap = argparse.ArgumentParser(description="Verify a jloop finalize actually landed.")
    ap.add_argument("issue")
    ap.add_argument("--url", help="PR URL that must appear in the body or attachments")
    ap.add_argument("--expect-labels", help="Comma-separated labels that must be present")
    args = ap.parse_args()

    key = token()
    if not key:
        print(json.dumps({"ok": False, "error": f"no Linear token in {' / '.join(TOKEN_VARS)}"}))
        return 2

    got = fetch(args.issue, key)
    if "errors" in got:
        print(json.dumps({"ok": False, "error": got["errors"][0].get("message")}))
        return 6
    node = (got.get("data") or {}).get("issue")
    if not node:
        print(json.dumps({"ok": False, "error": f"issue {args.issue} not found"}))
        return 6

    desc = node.get("description") or ""
    labels = [n["name"] for n in node["labels"]["nodes"]]
    urls = [n["url"] for n in node["attachments"]["nodes"]]

    problems = []
    if not any(m in desc for m in CALLOUT_MARKERS):
        problems.append(
            "callout missing from description -- merge_signal.py's new_description "
            "half was never applied (setting labels by hand does not do it)"
        )
    if args.url and args.url not in desc and args.url not in urls:
        problems.append(f"PR link {args.url} is in neither the description nor the attachments")
    if args.expect_labels:
        missing = [l for l in args.expect_labels.split(",") if l.strip() and l.strip() not in labels]
        if missing:
            problems.append(f"expected labels absent: {', '.join(missing)}")

    out = {
        "ok": not problems,
        "issue": node["identifier"],
        "state": node["state"]["name"],
        "labels": labels,
        "attachments": len(urls),
        "callout": any(m in desc for m in CALLOUT_MARKERS),
    }
    if problems:
        out["problems"] = problems
    print(json.dumps(out, indent=2))
    return 0 if not problems else 7


if __name__ == "__main__":
    sys.exit(main())
