#!/usr/bin/env python3
"""AI code review bridge: git diff -> Dify workflow (chunked, parallel) -> merged verdict.

Large diffs are split at file boundaries into chunks under a char budget,
reviewed in parallel, then merged deterministically. Advisory by default:
never fails the build unless DIFY_BLOCKING=1 and any chunk verdict is 'fail'
(or a chunk could not be reviewed). Infrastructure errors degrade to a
skipped review (exit 0) so the deterministic gate remains the only hard
blocker outside blocking mode.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DIFY_URL = os.environ.get("DIFY_URL", "http://localhost:80")
KEY_FILE = os.path.expanduser("~/.config/code-review/dify_api_key")
BLOCKING = os.environ.get("DIFY_BLOCKING", "") == "1"
RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai-review", "rules.md")

CHUNK_CHAR_BUDGET = int(os.environ.get("AI_REVIEW_CHUNK_CHARS", "100000"))
FILE_LINE_CAP = int(os.environ.get("AI_REVIEW_FILE_LINES", "800"))
MAX_CHUNKS = int(os.environ.get("AI_REVIEW_MAX_CHUNKS", "40"))
MAX_WORKERS = int(os.environ.get("AI_REVIEW_WORKERS", "3"))
SKIP_RE = re.compile(r"(^|/)(package-lock\.json|.*\.lock)$|^(dist|build|vendor|target)/|generated", re.I)
DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.M)


def sh(args):
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def get_raw_diff():
    head = os.environ.get("GIT_COMMIT")
    prev = os.environ.get("GIT_PREVIOUS_COMMIT")
    if head and prev:
        return sh(["git", "diff", prev, head])
    return sh(["git", "diff", "HEAD~1", "HEAD"])


def split_files(raw_diff):
    parts = DIFF_SPLIT_RE.split(raw_diff)
    return [p for p in (part.strip() for part in parts) if p]


def file_path_of(patch):
    m = re.search(r"^diff --git a/(\S+) b/(\S+)$", patch, re.M)
    return (m.group(2) if m else "unknown"), m is not None


def cap_patch(patch):
    lines = patch.splitlines()
    if len(lines) <= FILE_LINE_CAP:
        return patch, False
    return "\n".join(lines[:FILE_LINE_CAP]) + "\n... (该文件 diff 超过 %d 行，已截断)" % FILE_LINE_CAP, True


def build_chunks(files):
    chunks, current, size = [], [], 0
    for item in files:
        if current and size + len(item[1]) > CHUNK_CHAR_BUDGET:
            chunks.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item[1])
    if current:
        chunks.append(current)
    return chunks


def read_text(path):
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def review_chunk(rules, key, index, chunk):
    diff_text = "\n".join(p for _, p, _ in chunk)
    payload = json.dumps({
        "inputs": {"diff": diff_text, "rules": rules},
        "response_mode": "blocking",
        "user": "jenkins",
    }).encode()
    req = urllib.request.Request(
        DIFY_URL + "/v1/workflows/run",
        data=payload,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    files = [p for p, _, _ in chunk]

    def failed(reason):
        return {"_error": reason, "_files": files, "verdict": "pass", "summary": "分片未完成审核", "findings": []}

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.load(resp)
    except Exception as e:
        return failed("Dify 调用失败: %s" % e)
    data = body.get("data") or {}
    if data.get("status") != "succeeded":
        return failed("工作流状态 %s: %s" % (data.get("status"), str(data.get("error"))[:200]))
    out = data.get("outputs") or {}
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
            return failed("模型输出解析失败: %s" % raw[:200])
    result.setdefault("verdict", "pass")
    result.setdefault("findings", [])
    result["_files"] = files
    return result


def main():
    raw_diff = get_raw_diff()
    if not raw_diff.strip():
        print("AI review: no diff to review (skipped)")
        return 0

    files, skipped = [], []
    for patch in split_files(raw_diff):
        path, ok = file_path_of(patch)
        if not ok or SKIP_RE.search(path):
            skipped.append(path)
            continue
        patch, truncated = cap_patch(patch)
        files.append((path, patch, truncated))
    if not files:
        print("AI review: all changed files are out of review scope (skipped)")
        return 0

    chunks = build_chunks(files)
    if len(chunks) > MAX_CHUNKS:
        skipped += [p for c in chunks[MAX_CHUNKS:] for p, _, _ in c]
        chunks = chunks[:MAX_CHUNKS]

    rules = read_text(RULES)
    key = read_text(KEY_FILE).strip()
    if not key:
        print("AI review skipped: no Dify API key at %s" % KEY_FILE)
        return 0

    print("AI review: %d 个文件 -> %d 个分片（并行 %d）" % (len(files), len(chunks), MAX_WORKERS))
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(review_chunk, rules, key, i, c): (i, c) for i, c in enumerate(chunks, 1)}
        for fut in as_completed(futures):
            i, c = futures[fut]
            r = fut.result()
            r["_index"] = i
            results.append(r)
            names = ",".join(p for p, _, _ in c)[:70]
            if r.get("_error"):
                print("  分片 %d/%d (%s): 未完成 - %s" % (i, len(chunks), names, r["_error"]))
            else:
                print("  分片 %d/%d (%s): %s (%d findings)" % (i, len(chunks), names, r.get("verdict"), len(r.get("findings", []))))
    results.sort(key=lambda r: r["_index"])

    findings, seen = [], set()
    for r in results:
        for f in r.get("findings", []):
            dedupe_key = (f.get("file"), f.get("line"), str(f.get("message"))[:50])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            findings.append(f)
    unreviewed = [r for r in results if r.get("_error")]
    verdict = "fail" if any(r.get("verdict") == "fail" for r in results) else "pass"
    if unreviewed and BLOCKING:
        verdict = "fail"
        findings.append({
            "file": "-", "line": 0, "severity": "major",
            "message": "部分分片未能完成 AI 审核: " + "; ".join(r["_error"] for r in unreviewed),
            "suggestion": "检查 Dify 服务后重新触发构建",
        })
    truncated = [p for p, _, t in files if t]
    if skipped:
        print("AI review 跳过文件: %s%s" % (", ".join(skipped[:8]), "..." if len(skipped) > 8 else ""))

    result = {
        "verdict": verdict,
        "summary": "%d 个文件 / %d 个分片: %d 条发现%s%s" % (
            len(files), len(results), len(findings),
            "，%d 个分片未完成审核" % len(unreviewed) if unreviewed else "",
            "，%d 个文件超长截断" % len(truncated) if truncated else "",
        ),
        "findings": findings,
        "meta": {
            "chunks": len(results), "unreviewed_chunks": len(unreviewed),
            "files": len(files), "skipped_files": skipped,
            "truncated_files": truncated,
        },
    }
    with open("ai-review-result.json", "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("AI review verdict: %s (%d findings)" % (verdict, len(findings)))
    print("summary:", result["summary"])
    for f_ in findings[:12]:
        print("  [%s] %s:%s %s" % (f_.get("severity"), f_.get("file"), f_.get("line", "-"), str(f_.get("message"))[:90]))
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
