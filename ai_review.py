#!/usr/bin/env python3
"""AI code review bridge: git diff -> Dify workflow (chunked, parallel) -> merged verdict.

Changed files are grouped into fixed-budget review tasks (per task: the
file's diff, surrounding context from HEAD and scanner hints; oversized
files are further split by hunk groups), reviewed in parallel, then merged
deterministically. Per-call input is bounded regardless of change size. Advisory by default.
With DIFY_BLOCKING=1 the build fails only on blocker/critical-severity
findings (AI_REVIEW_BLOCK_SEVERITIES overrides the set) or when a chunk
could not be reviewed or files overflowed the chunk limit; major and below
stay display-only. Blocking-severity findings persist per branch in
.ai-review-state.<branch>.json (workspace-local, gitignored): each build
re-checks them against the code at HEAD — flagged code still present means
未修复 (re-reported and still blocking), code changed or file gone means
已修复 (dropped from the ledger). Fix status is thus decided by code
content, not by re-scanning the old commit. Infrastructure errors degrade to a skipped review
(exit 0) so the deterministic gate remains the only hard blocker outside
blocking mode.

Results are merged into the unified build report: this script writes the
ai_review section to scan/ai.json (make_report.py renders report.html;
the quality-gate section comes from gate.sh via scan/gate.json).

The diff base is tracked per branch across builds (.ai-review-base.<branch>
in the job workspace; the legacy .ai-review-base is migrated on first use)
and advanced only after a fully reviewed run, so commits pushed between
builds are never skipped. GIT_PREVIOUS_COMMIT /
GIT_PREVIOUS_SUCCESSFUL_COMMIT and HEAD~1 serve as fallbacks. Rebuilds with
no new diff still re-enforce the ledger, so unfixed blocking findings keep
failing the build until the code is actually changed.
"""
import json
import os
import re
import subprocess
import sys
import time
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


def current_branch():
    """构建分支：优先取工作区实际签出的分支（git HEAD）；游离 HEAD 时退回
    Jenkins 注入的 GIT_BRANCH/BRANCH_NAME，再不行用短 SHA。
    不先信任 GIT_BRANCH——它反映任务 SCM 配置的分支，而构建参数可在
    Checkout 阶段切到别的分支。"""
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=30)
    ref = r.stdout.strip()
    if ref and ref != "HEAD":
        return ref
    for env in ("GIT_BRANCH", "BRANCH_NAME"):
        v = os.environ.get(env, "").strip()
        if v:
            return v[len("origin/"):] if v.startswith("origin/") else v
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or "unknown"


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BRANCH = current_branch()
# 基点与台账按分支隔离，切换分支互不串扰；分支名做文件名安全替换
BRANCH_KEY = re.sub(r"[^A-Za-z0-9._-]", "_", BRANCH)
RULES = os.path.join(SCRIPT_DIR, "ai-review", "rules.md")
BASE_MARKER = os.path.join(SCRIPT_DIR, ".ai-review-base." + BRANCH_KEY)
STATE_FILE = os.path.join(SCRIPT_DIR, ".ai-review-state." + BRANCH_KEY + ".json")
LEGACY_BASE_MARKER = os.path.join(SCRIPT_DIR, ".ai-review-base")

def _int_env(*names, default):
    for name in names:
        v = os.environ.get(name, "").strip()
        if v:
            try:
                return int(v)
            except ValueError:
                print("警告: 环境变量 %s=%r 不是整数，使用默认值 %s" % (name, v, default))
    return default


# 每个审核任务的输入预算（字符）：diff+上下文+线索合计的常数上界，与变更总量无关；
# AI_REVIEW_CHUNK_CHARS 为旧环境变量名，继续兼容
TASK_BUDGET = _int_env("AI_REVIEW_TASK_BUDGET", "AI_REVIEW_CHUNK_CHARS", default=12000)
FILE_LINE_CAP = _int_env("AI_REVIEW_FILE_LINES", default=800)
CONTEXT_LINES = _int_env("AI_REVIEW_CONTEXT_LINES", default=40)
CONTEXT_BUDGET = _int_env("AI_REVIEW_CONTEXT_CHARS", default=4000)
HINTS_BUDGET = _int_env("AI_REVIEW_HINTS_CHARS", default=800)
MAX_CHUNKS = _int_env("AI_REVIEW_MAX_TASKS", "AI_REVIEW_MAX_CHUNKS", default=40)
MAX_WORKERS = _int_env("AI_REVIEW_WORKERS", default=6)
SKIP_RE = re.compile(
    r"(^|/)(package-lock\.json|.*\.lock)$"
    r"|^(dist|build|vendor|target)(/|$)"
    r"|(^|/)generated(/|$)"
    r"|_pb2\.(py|go)$"
    r"|\.min\.(js|css)$"
)
DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.M)
# 二进制标记只认 diff 头部元信息行（@@ 之前）；在整段 patch 上做子串匹配会把
# 本脚本源码里的字面量误当二进制标记，导致 ai_review.py 自身被跳过漏审
BINARY_RE = re.compile(r"^(?:GIT binary patch|Binary files .+ differ)$", re.M)


def sh(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout.strip()


def rev_parse(rev):
    r = subprocess.run(["git", "rev-parse", "--verify", rev + "^{commit}"], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else ""


def merge_base(a, b):
    r = subprocess.run(["git", "merge-base", a, b], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else ""


def resolve_base():
    """Review base commit: last fully reviewed HEAD from the workspace marker,
    else Jenkins-injected previous commit, else HEAD~1. merge-base guards
    against rewritten history; unresolvable candidates fall through."""
    head = rev_parse("HEAD")
    candidates = []
    if os.path.exists(BASE_MARKER):
        marker = read_text(BASE_MARKER).strip()
        if marker:
            candidates.append((marker, "%s（上次已审基点）" % os.path.basename(BASE_MARKER)))
    elif os.path.exists(LEGACY_BASE_MARKER):
        marker = read_text(LEGACY_BASE_MARKER).strip()
        if marker:
            candidates.append((marker, "旧版 .ai-review-base（迁移）"))
    for env in ("GIT_PREVIOUS_COMMIT", "GIT_PREVIOUS_SUCCESSFUL_COMMIT"):
        v = os.environ.get(env, "").strip()
        if v:
            candidates.append((v, env))
    for rev, source in candidates:
        base = merge_base(rev, head) if head else ""
        if base:
            return base, source
    return "HEAD~1", "默认 HEAD~1（首次构建或无可用基点）"


def get_review_range():
    base, source = resolve_base()
    head = rev_parse("HEAD")
    diff = sh(["git", "diff", base, head]) if head else ""
    return diff, base, head, source


def split_files(raw_diff):
    parts = DIFF_SPLIT_RE.split(raw_diff)
    return [p for p in (part.strip() for part in parts) if p]


def file_path_of(patch):
    m = re.search(r"^diff --git a/(\S+) b/(\S+)$", patch, re.M)
    return (m.group(2) if m else "unknown"), m is not None


def cap_patch(patch):
    """行数兜底截断；字符体量由任务预算 + hunk 拆分负责。"""
    truncated = False
    lines = patch.splitlines()
    if len(lines) > FILE_LINE_CAP:
        patch = "\n".join(lines[:FILE_LINE_CAP])
        truncated = True
    if truncated:
        patch += "\n... (该文件 diff 超长，已截断)"
    return patch, truncated


def severity_of(finding):
    return str(finding.get("severity") or "").strip().lower()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return []
    entries = state.get("blocking_findings") if isinstance(state, dict) else None
    return [e for e in (entries or []) if isinstance(e, dict)]


def save_state(entries):
    tmp = STATE_FILE + ".tmp"  # 原子写：中断不会损坏台账
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"blocking_findings": entries}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def file_lines_at(path, head):
    try:
        r = subprocess.run(["git", "show", "%s:%s" % (head, path)], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace").splitlines()


def context_window(lines, line_no):
    """标记行及其前后各一行的指纹（rstrip 对齐）；文件边缘时平移窗口保持 3 行，
    避免指纹缩成 1-2 行造成误匹配；返回 (窗口, 标记行偏移)。"""
    radius = 1
    if len(lines) < 2 * radius + 1:
        return [l.rstrip() for l in lines], line_no - 1
    i = min(max(line_no - 1, radius), len(lines) - 1 - radius)
    lo, hi = i - radius, i + radius + 1
    return [l.rstrip() for l in lines[lo:hi]], (line_no - 1) - lo


def find_context(lines, ctx, offset, near=None):
    """滑动窗口精确匹配指纹，命中返回标记行号（1-based），未命中 None；
    多处命中时取最接近上次记录行号（near）的，避免重复代码块错位。"""
    n = len(ctx)
    if not lines or n == 0 or len(lines) < n:
        return None
    offset = max(0, min(offset, n - 1))
    matches = [i for i in range(len(lines) - n + 1)
               if all(lines[i + k].rstrip() == ctx[k] for k in range(n))]
    if not matches:
        return None
    if near is None:
        return matches[0] + offset + 1
    return min((i + offset + 1 for i in matches), key=lambda ln: abs(ln - near))


def ledger_entry_from(f, head):
    """阻断级发现 -> 台账条目；定位不到 HEAD 文件内容的元发现（file=-/无行号）不落账。"""
    path = str(f.get("file") or "").strip()
    try:
        line_no = int(str(f.get("line", 0)).strip())
    except ValueError:
        line_no = 0
    if not path or path == "-" or line_no < 1 or not head:
        return None
    lines = file_lines_at(path, head)
    if not lines:
        return None
    ctx, offset = context_window(lines, line_no)
    return {
        "file": path, "line": line_no, "severity": severity_of(f),
        "message": str(f.get("message") or ""), "suggestion": str(f.get("suggestion") or ""),
        "context": ctx, "ctx_offset": offset,
        "first_seen_commit": head[:8], "first_seen_build": os.environ.get("BUILD_NUMBER", "-"),
        "last_seen_commit": head[:8],
    }


def same_issue(a, e):
    if a.get("file") != e.get("file"):
        return False
    if a.get("context") and e.get("context") and a["context"] == e["context"]:
        return True
    return str(a.get("message", ""))[:60] == str(e.get("message", ""))[:60]


def recheck_ledger(entries, head):
    """对照 HEAD 代码核对台账：指纹仍在 = 未修复（行号刷新），已变/文件删 = 已修复。"""
    if not head:
        return entries, []
    contents, still, resolved = {}, [], []
    for e in entries:
        path = str(e.get("file") or "")
        if path not in contents:
            contents[path] = file_lines_at(path, head)
        at = find_context(contents[path], e.get("context") or [], int(e.get("ctx_offset", 0) or 0), near=e.get("line"))
        if at:
            still.append(dict(e, line=at, last_seen_commit=head[:8]))
        else:
            resolved.append(e)
    return still, resolved


def carried_copy(e):
    """台账条目 -> 报告发现行（标注未修复与首次发现构建号）。"""
    seen_at = e.get("first_seen_build")
    label = "首次发现于构建 %s" % seen_at if seen_at not in (None, "", "-") else "历史遗留"
    return {
        "file": e.get("file"), "line": e.get("line"), "severity": e.get("severity"),
        "message": "%s（%s，未修复）" % (e.get("message"), label),
        "suggestion": e.get("suggestion", ""),
        "carried": True,
    }


HUNK_HEAD_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def split_patch_hunks(patch):
    """按 hunk 切分 patch，返回 (header, [hunk_text, ...])。"""
    header, hunks, cur = [], [], None
    for line in patch.splitlines(keepends=True):
        if line.startswith("@@"):
            if cur:
                hunks.append("".join(cur))
            cur = [line]
        elif cur is not None:
            cur.append(line)
        else:
            header.append(line)
    if cur:
        hunks.append("".join(cur))
    return "".join(header), hunks


def file_context(path, patch, head):
    """变更 hunk 在 HEAD 文件上的周边代码窗口（默认 ±40 行，多 hunk 区间合并，
    行号即文件真实行号）；新文件或 HEAD 读不到时回退为 diff 的新增行。
    超出 CONTEXT_BUDGET 截断。"""
    _, hunks = split_patch_hunks(patch)
    ranges = []
    for h in hunks:
        m = HUNK_HEAD_RE.match(h)
        start = int(m.group(1)) if m else 1
        count = int(m.group(2) or "1") if m else 1
        end = start + max(count, 1) - 1
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    lines = file_lines_at(path, head)
    if lines:
        segs = []
        for start, end in ranges:
            lo, hi = max(1, start - CONTEXT_LINES), min(len(lines), end + CONTEXT_LINES)
            segs.append("[上下文 %s HEAD 第 %d-%d 行]\n%s" % (
                path, lo, hi,
                "\n".join("%5d| %s" % (i, lines[i - 1]) for i in range(lo, hi + 1))))
        text = "\n".join(segs)
    else:
        added = "\n".join(l[1:] for l in patch.splitlines()
                          if l.startswith("+") and not l.startswith("+++"))
        text = "[上下文 %s（新文件，取 diff 新增行）]\n%s" % (path, added)
    if len(text) > CONTEXT_BUDGET:
        text = text[:CONTEXT_BUDGET] + "\n... (上下文超预算，已截断)"
    return text


def load_hints_by_file():
    """scan/semgrep.json -> {path: 线索文本}；报告缺失或损坏时返回空。"""
    try:
        with open("scan/semgrep.json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    hits = {}
    for r in data.get("results", []) if isinstance(data, dict) else []:
        path = r.get("path")
        if not path:
            continue
        extra = r.get("extra") or {}
        hits.setdefault(path, []).append("- [%s] %s:%s %s (%s)" % (
            extra.get("severity", "?"), path, (r.get("start") or {}).get("line", 0),
            str(extra.get("message", ""))[:100], r.get("check_id", "?")))
    return {p: "\n".join(v)[:HINTS_BUDGET] for p, v in hits.items()}


def file_section(path, patch, ctx, hints):
    sec = "== 文件: %s ==\n[变更 diff]\n%s" % (path, patch)
    if ctx:
        sec += "\n\n" + ctx
    if hints:
        sec += "\n\n[静态扫描线索]\n%s" % hints
    return sec


def build_material_items(path, patch, truncated, head, hints):
    """一个文件 -> 一个或多个任务项（超预算时按 hunk 组拆段、单 hunk 再按行切），
    保证每项 section 的长度 ≤ TASK_BUDGET。"""
    ctx = file_context(path, patch, head)
    hint = hints.get(path, "")
    fixed = len(file_section(path, "", ctx, hint))
    budget = max(TASK_BUDGET - fixed, 500)
    if len(patch) <= budget:
        section = file_section(path, patch, ctx, hint)
        return [{"path": path, "section": section, "truncated": truncated, "size": len(section)}]

    header, hunks = split_patch_hunks(patch)
    units = []
    for h in hunks or [patch]:
        if len(h) <= budget:
            units.append(h)
            continue
        cur, size = [], 0  # 单 hunk 仍超预算：按行切
        for ln in h.splitlines(keepends=True):
            if cur and size + len(ln) > budget:
                units.append("".join(cur))
                cur, size = [], 0
            cur.append(ln)
            size += len(ln)
        if cur:
            units.append("".join(cur))
    groups, cur, size = [], [], 0
    for u in units:
        if cur and size + len(u) > budget:
            groups.append("".join(cur))
            cur, size = [], 0
        cur.append(u)
        size += len(u)
    if cur:
        groups.append("".join(cur))

    items = []
    total = len(groups)
    for i, grp in enumerate(groups, 1):
        note = "" if total == 1 else "\n[说明] 该文件 diff 过长，本任务仅包含第 %d/%d 段 hunks" % (i, total)
        section = file_section(path, header + grp, ctx if i == 1 else "", hint if i == 1 else "") + note
        items.append({"path": path, "section": section, "truncated": truncated, "size": 0})
    for it in items:
        it["size"] = len(it["section"])
    return items


def build_tasks(items):
    """贪心装箱：相邻任务项在预算内合并成一个任务，减少调用次数。"""
    tasks = []
    for it in items:
        if tasks and tasks[-1]["size"] + it["size"] <= TASK_BUDGET:
            tasks[-1]["items"].append(it)
            tasks[-1]["size"] += it["size"]
        else:
            tasks.append({"items": [it], "size": it["size"]})
    return tasks


def read_text(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""


def save_ai_result(ai_section):
    """Write the AI section to scan/ai.json (make_report.py merges it into report.html)."""
    os.makedirs('scan', exist_ok=True)
    with open('scan/ai.json', 'w') as f:
        json.dump(ai_section, f, ensure_ascii=False, indent=2)


def finish_without_scan(reason, head, extra_meta=None):
    """无可审 diff 时先核对遗留台账：未修复的阻断级发现继续生效（blocking 时阻断），
    避免无新提交的空重建绕过"不修不放行"。"""
    still_open, resolved = recheck_ledger(load_state(), head)
    save_state(still_open)
    meta = {"branch": BRANCH, "carried_findings": len(still_open), "resolved_findings": len(resolved)}
    if extra_meta:
        meta.update(extra_meta)
    if not still_open:
        save_ai_result({"verdict": "skipped", "reason": reason, "meta": meta})
        print("AI review: %s (skipped)" % reason)
        return 0
    save_ai_result({
        "verdict": "fail",
        "summary": "%s；%d 条遗留发现未修复" % (reason, len(still_open)),
        "findings": [carried_copy(e) for e in still_open],
        "meta": meta,
    })
    print("AI review: %s；%d 条遗留发现未修复" % (reason, len(still_open)))
    if BLOCKING:
        print("AI review: blocking mode -> failing build (%d 条历史遗留未修复)" % len(still_open))
        return 1
    return 0


def review_chunk(rules, key, index, task):
    diff_text = "\n\n".join(it["section"] for it in task["items"])
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
    files = list(dict.fromkeys(it["path"] for it in task["items"]))

    def failed(reason):
        return {"_error": reason, "_files": files, "verdict": "pass", "summary": "任务未完成审核", "findings": []}

    # 调用与解析整体重试（最多 3 次尝试）：LLM 偶发输出截断/流中断是瞬时的，
    # 直接判定任务未完成会让 blocking 构建无谓失败
    result, last_reason = None, ""
    for attempt in (1, 2, 3):
        if attempt > 1 and result is None:
            print("  任务 %d: 第 %d 次调用未得到可用输出，重试" % (index, attempt - 1))
            time.sleep(2 * (attempt - 1))
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                body = json.load(resp)
        except Exception as e:
            last_reason = "Dify 调用失败: %s" % e
            continue
        data = body.get("data") or {}
        if data.get("status") != "succeeded":
            last_reason = "工作流状态 %s: %s" % (data.get("status"), str(data.get("error"))[:200])
            continue
        out = data.get("outputs") or {}
        raw = out.get("review") or out.get("text") or ""
        if isinstance(raw, dict):
            result = raw
            break
        raw = str(raw).strip()
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        try:
            result = json.loads(raw)
        except Exception:
            # LLM 常在字符串值里输出未转义的控制字符（如裸换行），strict 模式会拒绝
            try:
                result = json.loads(raw, strict=False)
            except Exception as e:
                last_reason = "模型输出解析失败: %s (%s)" % (e, raw[:200])
                continue
        if not isinstance(result, dict):
            last_reason = "模型输出非 JSON 对象（类型 %s）: %s" % (type(result).__name__, str(result)[:200])
            result = None
            continue
        break
    if result is None:
        return failed("%s（重试 2 次后仍失败）" % last_reason)
    result.setdefault("verdict", "pass")
    result.setdefault("findings", [])
    result["_files"] = files
    return result


def main():
    raw_diff, base, head, base_source = get_review_range()
    print("AI review 分支: %s" % BRANCH)
    print("AI review 范围: %s..%s（基点: %s）" % (base[:8], head[:8] or "HEAD", base_source))
    if not raw_diff.strip():
        return finish_without_scan("无新增 diff", head)

    files, skipped = [], {}
    for patch in split_files(raw_diff):
        path, ok = file_path_of(patch)
        if not ok:
            skipped[path] = "无法解析"
            continue
        if SKIP_RE.search(path):
            skipped[path] = "范围外文件"
            continue
        if BINARY_RE.search(patch.split("\n@@", 1)[0]):
            skipped[path] = "二进制文件"
            continue
        if not any(l.startswith("+") and not l.startswith("+++") for l in patch.splitlines()):
            skipped[path] = "纯删除"
            continue
        patch, truncated = cap_patch(patch)
        files.append((path, patch, truncated))
    if not files:
        return finish_without_scan("全部文件在审查范围外", head,
                                   {"skipped": [{"file": p, "reason": r} for p, r in skipped.items()]})

    hints = load_hints_by_file()
    items = []
    for path, patch, t in files:
        items.extend(build_material_items(path, patch, t, head, hints))
    chunks = build_tasks(items)
    overflow_files = []
    if len(chunks) > MAX_CHUNKS:
        overflow_files = list(dict.fromkeys(
            it["path"] for c in chunks[MAX_CHUNKS:] for it in c["items"]))
        chunks = chunks[:MAX_CHUNKS]

    rules = read_text(RULES)
    key = read_text(KEY_FILE).strip()
    if not key:
        print("AI review skipped: no Dify API key at %s" % KEY_FILE)
        save_ai_result({"verdict": "skipped", "reason": "未配置 Dify API key", "skipped": skipped})
        return 0

    print("AI review: %d 个文件 -> %d 个任务（并行 %d，单任务预算 %d 字符）" % (
        len(files), len(chunks), MAX_WORKERS, TASK_BUDGET))
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(review_chunk, rules, key, i, c): (i, c) for i, c in enumerate(chunks, 1)}
        for fut in as_completed(futures):
            i, c = futures[fut]
            r = fut.result()
            r["_index"] = i
            results.append(r)
            names = ",".join(dict.fromkeys(it["path"] for it in c["items"]))[:70]
            if r.get("_error"):
                print("  任务 %d/%d (%s): 未完成 - %s" % (i, len(chunks), names, r["_error"]))
            else:
                print("  任务 %d/%d (%s): %s (%d findings)" % (i, len(chunks), names, r.get("verdict"), len(r.get("findings", []))))
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
            "message": "%d 个任务未能完成 AI 审核: %s" % (len(unreviewed), "; ".join(r["_error"] for r in unreviewed)[:200]),
            "suggestion": "检查 Dify 服务后重新触发构建",
        })
    if overflow_files:
        findings.append({
            "file": "-", "line": 0, "severity": "major",
            "message": "%d 个文件超出任务上限未审核: %s" % (len(overflow_files), ", ".join(overflow_files[:5])),
            "suggestion": "提高 AI_REVIEW_MAX_TASKS 或拆分提交",
        })
    truncated = [p for p, _, t in files if t]

    # 遗留台账：上次的阻断级发现逐一对照 HEAD 代码——代码仍在 = 未修复（并入本轮
    # 报告并继续阻断），已变更或文件删除 = 已修复（出账）。台账与审核基点解耦：
    # 新提交照常增量审，遗留问题不依赖 LLM 重扫，修复与否由代码内容判定。
    state = load_state()
    still_open, resolved = recheck_ledger(state, head)
    fresh_ledger, matched = [], set()
    for f in (f for f in findings if severity_of(f) in BLOCK_SEVERITIES):
        entry = ledger_entry_from(f, head)
        if not entry:
            continue
        hit = next((i for i, e in enumerate(still_open) if i not in matched and same_issue(entry, e)), None)
        if hit is None:
            fresh_ledger.append(entry)
        else:
            # 本轮扫描重新命中的遗留条目：以本轮信息刷新，保留首次发现时间
            still_open[hit] = dict(entry,
                                   first_seen_commit=still_open[hit].get("first_seen_commit", ""),
                                   first_seen_build=still_open[hit].get("first_seen_build", "-"))
            matched.add(hit)
    carried = []
    for i, e in enumerate(still_open):
        if i not in matched:
            findings.append(carried_copy(e))
            carried.append(e)
    save_state(still_open + fresh_ledger)

    verdict = "fail" if any(r.get("verdict") == "fail" for r in results) or still_open else "pass"
    hard_findings = [f for f in findings if severity_of(f) in BLOCK_SEVERITIES]
    if skipped:
        print("AI review 跳过文件: %s" % "; ".join("%s(%s)" % (p, reason) for p, reason in list(skipped.items())[:8]))

    result = {
        "verdict": verdict,
        "summary": "%d 个文件 / %d 个任务: %d 条发现%s%s%s%s%s" % (
            len(files), len(results), len(findings),
            "，%d 个任务未完成审核" % len(unreviewed) if unreviewed else "",
            "，%d 个文件超出任务上限" % len(overflow_files) if overflow_files else "",
            "，%d 个文件超长截断" % len(truncated) if truncated else "",
            "，%d 条遗留发现未修复" % len(carried) if carried else "",
            "，%d 条遗留发现已修复" % len(resolved) if resolved else "",
        ),
        "findings": findings,
        "meta": {
            "branch": BRANCH,
            "chunks": len(results), "unreviewed_chunks": len(unreviewed),
            "files": len(files),
            "skipped": [{"file": p, "reason": reason} for p, reason in skipped.items()],
            "truncated_files": truncated,
            "overflow_files": overflow_files,
            "blocking_severities": sorted(BLOCK_SEVERITIES),
            "blocking_findings": len(hard_findings),
            "carried_findings": len(carried),
            "resolved_findings": len(resolved),
            "resolved_detail": ["%s:%s %s" % (e.get("file"), e.get("line"), str(e.get("message"))[:40]) for e in resolved][:8],
        },
    }
    save_ai_result({
        "verdict": verdict,
        "summary": result["summary"],
        "findings": findings,
        "meta": result["meta"],
    })
    # 基点只在整段范围都审完时推进：有任务未审完或文件超任务上限时，
    # 下次构建仍从旧基点重审，避免漏审
    if not overflow_files and not unreviewed and head:
        with open(BASE_MARKER, "w") as f:
            f.write(head + "\n")

    print("AI review verdict: %s (%d findings)" % (verdict, len(findings)))
    print("summary:", result["summary"])
    for f_ in findings[:12]:
        print("  [%s] %s:%s %s" % (f_.get("severity"), f_.get("file"), f_.get("line", "-"), str(f_.get("message"))[:90]))
    if BLOCKING:
        blocking_reasons = []
        if hard_findings:
            blocking_reasons.append("%d 条 %s 级发现%s" % (
                len(hard_findings), "/".join(sorted(BLOCK_SEVERITIES)),
                "（含 %d 条历史遗留未修复）" % len(carried) if carried else ""))
        if unreviewed:
            blocking_reasons.append("%d 个任务未完成审核" % len(unreviewed))
        if overflow_files:
            blocking_reasons.append("%d 个文件超出任务上限未审核" % len(overflow_files))
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
