#!/usr/bin/env python3
"""
jloop PR attachment — link a GitHub PR to its Linear issue, reliably.

Why this exists
---------------
Linear's GitHub integration can auto-link a PR to an issue, but only when the
integration is actually installed on the repo. It is a per-workspace, paid-plan,
admin-only setup. When it is absent, linking silently does nothing: no error, no
attachment, and the issue shows no PR. The `Closes TEAM-123` magic word in the PR
body does nothing either -- that phrase is consumed by the integration, so with
no integration there is nothing to consume it.

That failure mode is invisible, which makes it the worst kind. jloop is used
against repos whose integration status we cannot know, so we do not depend on it:
we create the attachment explicitly through the API. This works for every user
who has a Linear token, which jloop already requires.

Idempotent: if an attachment with the same URL already exists on the issue --
whether created by us on a previous run, or by the GitHub integration for users
who do have it installed -- we leave it alone rather than creating a duplicate.

Usage:
  attach_pr.py TEAM-123 --url https://github.com/org/repo/pull/42 \
      [--title "PR #42 -- fix(TEAM-123): ..."] [--quiet]

Exit codes: 0 success (created or already present); 2 bad usage / missing token;
6 Linear API error. Non-zero from a build step should be treated as "the PR is
not linked", not as a reason to fail the whole build -- see main() for rationale.

Token is read from LINEAR_API_KEY, falling back to MCP_LINEAR_API_KEY.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.linear.app/graphql"
TOKEN_VARS = ("LINEAR_API_KEY", "MCP_LINEAR_API_KEY")

# NOTE: attachmentLinkURL takes issueId, url and title. It does NOT accept a
# `subtitle` argument -- passing one is a hard GraphQL validation error.
MUTATION = """
mutation($id:String!,$url:String!,$title:String!){
  attachmentLinkURL(issueId:$id,url:$url,title:$title){
    success
    attachment{ id title url }
  }
}
"""

QUERY = """
query($id:String!){
  issue(id:$id){
    id
    identifier
    attachments{ nodes{ id url title sourceType } }
  }
}
"""


def token():
    for var in TOKEN_VARS:
        val = os.environ.get(var)
        if val:
            return val
    return None


def gql(query, variables, key):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API,
        data=payload,
        headers={"Authorization": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": f"HTTP {e.code}: {e.read().decode()[:300]}"}]}
    except Exception as e:  # network, DNS, timeout
        return {"errors": [{"message": f"{type(e).__name__}: {e}"}]}
    return body


def attach(issue, url, title, key) -> "tuple[str, object]":
    """Returns (status, detail) where status is one of:
    'exists' | 'created' | 'error'."""
    got = gql(QUERY, {"id": issue}, key)
    if "errors" in got:
        return "error", got["errors"][0].get("message", "unknown error")
    node = (got.get("data") or {}).get("issue")
    if not node:
        return "error", f"issue {issue} not found"

    for a in node["attachments"]["nodes"]:
        if a.get("url") == url:
            # Already linked -- by us, or by the GitHub integration for users
            # who have it. Either way there is nothing to do.
            return "exists", a
    res = gql(MUTATION, {"id": node["id"], "url": url, "title": title}, key)
    if "errors" in res:
        return "error", res["errors"][0].get("message", "unknown error")
    payload = (res.get("data") or {}).get("attachmentLinkURL") or {}
    if not payload.get("success"):
        return "error", "attachmentLinkURL returned success=false"
    return "created", payload.get("attachment")


def main():
    ap = argparse.ArgumentParser(description="Attach a GitHub PR link to a Linear issue.")
    ap.add_argument("issue", help="Issue identifier, e.g. GI-28")
    ap.add_argument("--url", required=True, help="Pull request URL")
    ap.add_argument("--title", default=None, help="Attachment title (defaults to the PR URL tail)")
    ap.add_argument("--quiet", action="store_true", help="Suppress the JSON result line")
    args = ap.parse_args()

    key = token()
    if not key:
        print(json.dumps({"ok": False, "error": f"no Linear token in {' / '.join(TOKEN_VARS)}"}))
        return 2

    title = args.title or f"PR {args.url.rstrip('/').rsplit('/', 1)[-1]} — {args.issue}"
    status, detail = attach(args.issue, args.url, title, key)

    out = {"ok": status != "error", "issue": args.issue, "url": args.url, "status": status}
    if status == "error":
        out["error"] = detail
    if not args.quiet:
        print(json.dumps(out))

    # A failure here means the PR is not linked in Linear. That is worth
    # surfacing loudly, but it is NOT a reason to discard a green build: the
    # code is already pushed and the PR already exists. Callers should warn on
    # non-zero rather than abort.
    return 0 if status != "error" else 6


if __name__ == "__main__":
    sys.exit(main())
