#!/usr/bin/env python3
"""AI code review bridge: git diff -> Dify workflow (chunked, parallel) -> merged verdict.

Large diffs are split at file boundaries into chunks under a char budget,
reviewed in parallel, then merged deterministically. Advisory by default.
With DIFY_BLOCKING=1 the build fails only on blocker/critical-severity
findings (AI_REVIEW_BLOCK_SEVERITIES overrides the set) or when a chunk
could not be reviewed or files overflowed the chunk limit; major and below
stay display-only. Infrastructure errors degrade to a skipped review
(exit 0) so the deterministic gate remains the only hard blocker outside
blocking mode.

Results are merged into report.json (unified build report; the quality-gate
section is written by gate.sh, this script only fills the ai_review part).
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

DIFY_URL = os.environ.get("DIFY_URL", "http://localhost:80")
KEY_FILE = os.path.expanduser("~/.config/code-review/dify_api_key")
BLOCKING = os.environ.get("DIFY_BLOCKING", "") == "1"
BLOCK_SEVERITIES = {
    s.strip().lower()
    for s in os.environ.get("AI_REVIEW_BLOCK_SEVERITIES", "blocker,critical").split(",")
    if s.strip()
}
RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai-review", "rules.md")

CHUNK_CHAR_BUDGET = int(os.environ.get("AI_REVIEW_CHUNK_CHARS", "100000"))
FILE_LINE_CAP = int(os.environ.get("AI_REVIEW_FILE_LINES", "800"))
MAX_CHUNKS = int(os.environ.get("AI_REVIEW_MAX_CHUNKS", "40"))
MAX_WORKERS = int(os.environ.get("AI_REVIEW_WORKERS", "3"))
SKIP_RE = re.compile(
    r"(^|/)(package-lock\.json|.*\.lock)$"
    r"|^(dist|build|vendor|target)(/|$)"
    r"|(^|/)generated(/|$)"
    r"|_pb2\.(py|go)$"
    r"|\.min\.(js|css)$"
)
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
    truncated = False
    lines = patch.splitlines()
    if len(lines) > FILE_LINE_CAP:
        patch = "\n".join(lines[:FILE_LINE_CAP])
        truncated = True
    char_cap = max(CHUNK_CHAR_BUDGET - 4000, 2000)
    if len(patch) > char_cap:
        cut = patch.rfind("\n", 0, char_cap)
        patch = patch[: cut if cut > 0 else char_cap]
        truncated = True
    if truncated:
        patch += "\n... (该文件 diff 超长，已截断)"
    return patch, truncated


def severity_of(finding):
    return str(finding.get("severity") or "").strip().lower()


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


def save_ai_result(ai_section):
    """Write the AI section to scan/ai.json (make_report.py merges it into report.html)."""
    os.makedirs('scan', exist_ok=True)
    with open('scan/ai.json', 'w') as f:
        json.dump(ai_section, f, ensure_ascii=False, indent=2)


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
        save_ai_result({"verdict": "skipped", "reason": "无 diff"})
        return 0

    files, skipped = [], {}
    for patch in split_files(raw_diff):
        path, ok = file_path_of(patch)
        if not ok:
            skipped[path] = "无法解析"
            continue
        if SKIP_RE.search(path):
            skipped[path] = "范围外文件"
            continue
        if "GIT binary patch" in patch or "\nBinary files " in patch:
            skipped[path] = "二进制文件"
            continue
        if not any(l.startswith("+") and not l.startswith("+++") for l in patch.splitlines()):
            skipped[path] = "纯删除"
            continue
        patch, truncated = cap_patch(patch)
        files.append((path, patch, truncated))
    if not files:
        print("AI review: all changed files are out of review scope (skipped)")
        save_ai_result({"verdict": "skipped", "reason": "全部文件在审查范围外", "skipped": skipped})
        return 0

    chunks = build_chunks(files)
    overflow_files = []
    if len(chunks) > MAX_CHUNKS:
        overflow_files = [p for c in chunks[MAX_CHUNKS:] for p, _, _ in c]
        chunks = chunks[:MAX_CHUNKS]

    rules = read_text(RULES)
    key = read_text(KEY_FILE).strip()
    if not key:
        print("AI review skipped: no Dify API key at %s" % KEY_FILE)
        save_ai_result({"verdict": "skipped", "reason": "未配置 Dify API key", "skipped": skipped})
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
    if unreviewed:
        findings.append({
            "file": "-", "line": 0, "severity": "major",
            "message": "%d 个分片未能完成 AI 审核: %s" % (len(unreviewed), "; ".join(r["_error"] for r in unreviewed)[:200]),
            "suggestion": "检查 Dify 服务后重新触发构建",
        })
    if overflow_files:
        findings.append({
            "file": "-", "line": 0, "severity": "major",
            "message": "%d 个文件超出分片上限未审核: %s" % (len(overflow_files), ", ".join(overflow_files[:5])),
            "suggestion": "提高 AI_REVIEW_MAX_CHUNKS 或拆分提交",
        })
    verdict = "fail" if any(r.get("verdict") == "fail" for r in results) else "pass"
    truncated = [p for p, _, t in files if t]
    hard_findings = [f for f in findings if severity_of(f) in BLOCK_SEVERITIES]
    if skipped:
        print("AI review 跳过文件: %s" % "; ".join("%s(%s)" % (p, reason) for p, reason in list(skipped.items())[:8]))

    result = {
        "verdict": verdict,
        "summary": "%d 个文件 / %d 个分片: %d 条发现%s%s%s" % (
            len(files), len(results), len(findings),
            "，%d 个分片未完成审核" % len(unreviewed) if unreviewed else "",
            "，%d 个文件超出分片上限" % len(overflow_files) if overflow_files else "",
            "，%d 个文件超长截断" % len(truncated) if truncated else "",
        ),
        "findings": findings,
        "meta": {
            "chunks": len(results), "unreviewed_chunks": len(unreviewed),
            "files": len(files),
            "skipped": [{"file": p, "reason": reason} for p, reason in skipped.items()],
            "truncated_files": truncated,
            "overflow_files": overflow_files,
            "blocking_severities": sorted(BLOCK_SEVERITIES),
            "blocking_findings": len(hard_findings),
        },
    }
    save_ai_result({
        "verdict": verdict,
        "summary": result["summary"],
        "findings": findings,
        "meta": result["meta"],
    })

    print("AI review verdict: %s (%d findings)" % (verdict, len(findings)))
    print("summary:", result["summary"])
    for f_ in findings[:12]:
        print("  [%s] %s:%s %s" % (f_.get("severity"), f_.get("file"), f_.get("line", "-"), str(f_.get("message"))[:90]))
    if BLOCKING:
        blocking_reasons = []
        if hard_findings:
            blocking_reasons.append("%d 条 %s 级发现" % (len(hard_findings), "/".join(sorted(BLOCK_SEVERITIES))))
        if unreviewed:
            blocking_reasons.append("%d 个分片未完成审核" % len(unreviewed))
        if overflow_files:
            blocking_reasons.append("%d 个文件超出分片上限未审核" % len(overflow_files))
        if blocking_reasons:
            print("AI review: blocking mode -> failing build (%s)" % "; ".join(blocking_reasons))
            return 1
        print("AI review: blocking mode, 无阻断级发现（major 以下仅展示）-> 继续构建")
        return 0
    print("AI review: advisory mode (set DIFY_BLOCKING=1 in Jenkins to enforce)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("AI review skipped: unexpected error: %s" % e)
        save_ai_result({"verdict": "skipped", "reason": "unexpected error: %s" % e})
        sys.exit(0)
