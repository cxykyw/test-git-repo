#!/usr/bin/env python3
"""AI code review bridge: git diff -> Dify workflow -> JSON verdict.

Advisory by default: never fails the build unless DIFY_BLOCKING=1 and the
review verdict is 'fail'. Any infrastructure error degrades to a skipped
review (exit 0) so the deterministic gate remains the only hard blocker.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

MAX_DIFF_LINES = 2000
DIFY_URL = os.environ.get("DIFY_URL", "http://localhost:80")
KEY_FILE = os.path.expanduser("~/.config/code-review/dify_api_key")
BLOCKING = os.environ.get("DIFY_BLOCKING", "") == "1"
RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai-review", "rules.md")


def sh(args):
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def get_diff():
    head = os.environ.get("GIT_COMMIT")
    prev = os.environ.get("GIT_PREVIOUS_COMMIT")
    if head and prev:
        diff = sh(["git", "diff", prev, head])
    else:
        diff = sh(["git", "diff", "HEAD~1", "HEAD"])
    return diff


def main():
    diff = get_diff()
    if not diff.strip():
        print("AI review: no diff to review (skipped)")
        return 0
    lines = diff.splitlines()
    if len(lines) > MAX_DIFF_LINES:
        diff = "\n".join(lines[:MAX_DIFF_LINES]) + "\n... (truncated, %d lines total)" % len(lines)

    rules = ""
    if os.path.exists(RULES):
        with open(RULES) as f:
            rules = f.read()

    key = ""
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            key = f.read().strip()
    if not key:
        print("AI review skipped: no Dify API key at %s" % KEY_FILE)
        return 0

    payload = json.dumps({
        "inputs": {"diff": diff, "rules": rules},
        "response_mode": "blocking",
        "user": "jenkins",
    }).encode()
    req = urllib.request.Request(
        DIFY_URL + "/v1/workflows/run",
        data=payload,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.load(resp)
    except Exception as e:
        print("AI review skipped: Dify call failed: %s" % e)
        return 0

    out = (body.get("data") or {}).get("outputs") or {}
    raw = out.get("review") or out.get("text") or ""
    if isinstance(raw, dict):
        result = raw
    else:
        raw = str(raw).strip()
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        try:
            result = json.loads(raw)
        except Exception:
            print("AI review skipped: unparseable model output:\n%s" % raw[:500])
            return 0

    findings = result.get("findings", [])
    verdict = result.get("verdict", "pass")
    with open("ai-review-result.json", "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("AI review verdict: %s (%d findings)" % (verdict, len(findings)))
    print("summary:", result.get("summary", ""))
    for f_ in findings[:10]:
        print("  [%s] %s:%s %s" % (f_.get("severity"), f_.get("file"), f_.get("line", "-"), f_.get("message")))
    if BLOCKING and verdict == "fail":
        print("AI review: blocking mode and verdict=fail -> failing build")
        return 1
    print("AI review: advisory mode (set DIFY_BLOCKING=1 in Jenkins to enforce)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("AI review skipped: unexpected error: %s" % e)
        sys.exit(0)
