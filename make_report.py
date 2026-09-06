#!/usr/bin/env python3
"""Assemble the human-readable build report (report.html).

Reads scan/gate.json (written by gate.sh) and scan/ai.json (written by
ai_review.py); either may be missing. Output is a self-contained HTML file
archived alongside the final zip artifact.
"""
import html
import json
import os
from datetime import datetime

SEV_ORDER = {"blocker": 0, "major": 1, "minor": 2, "info": 3}

TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>构建审核报告</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
      margin:32px auto;max-width:960px;color:#1f2328;line-height:1.55}
 h1{font-size:22px} h2{font-size:17px;border-bottom:2px solid #e8e8e8;padding-bottom:6px;margin-top:32px}
 table{border-collapse:collapse;width:100%;margin:10px 0}
 td,th{border:1px solid #e0e0e0;padding:7px 10px;text-align:left;vertical-align:top;font-size:14px}
 th{background:#f5f6f7}
 .badge{display:inline-block;padding:3px 12px;border-radius:12px;color:#fff;font-weight:600;font-size:13px}
 .ok{background:#1a7f37}.bad{background:#cf222e}.gray{background:#6e7781}
 .sev-blocker{background:#cf222e}.sev-major{background:#bc4c00}.sev-minor{background:#0969da}.sev-info{background:#6e7781}
 .muted{color:#656d76;font-size:13px}
 code{background:#f0f1f2;padding:1px 5px;border-radius:4px;font-size:13px}
 .num{text-align:right;font-variant-numeric:tabular-nums}
</style></head><body>
<h1>构建审核报告 <span class="muted">生成于 __NOW__</span></h1>
__GATE_SECTION__
__AI_SECTION__
<p class="muted">机器可读数据见构建工作区 scan/ 目录；本文件为最终归档产物之一。</p>
</body></html>
"""


def load(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("警告: %s 读取失败: %s" % (path, e))
        return None


def badge(text, kind):
    return '<span class="badge %s">%s</span>' % (kind, html.escape(str(text)))


def verdict_badge(verdict, pass_text, fail_text):
    if not verdict:
        return badge("未执行", "gray")
    if verdict == "PASS" or verdict == "pass":
        return badge(pass_text, "ok")
    if verdict in ("FAIL", "fail"):
        return badge(fail_text, "bad")
    return badge(str(verdict), "gray")


def gate_section(gate):
    if not gate:
        return "<h2>质量门禁</h2><p class=\"muted\">未执行。</p>"
    b = verdict_badge(gate.get("verdict"), "门禁通过", "门禁未通过")
    counts = "".join(
        "<tr><td>%s</td><td class=\"num\">%s</td></tr>" % (
            html.escape(label), html.escape(str(gate.get(key, 0))))
        for key, label in (
            ("semgrep_errors", "Semgrep ERROR"),
            ("semgrep_warnings", "Semgrep WARNING"),
            ("gitleaks_secrets", "Gitleaks 密钥"),
            ("trivy_critical_vulns", "Trivy CRITICAL 漏洞"),
        )
    )
    failures = gate.get("failures") or []
    fail_html = ""
    if failures:
        fail_html = "<p><b>失败原因：</b></p><ul>%s</ul>" % "".join(
            "<li>%s</li>" % html.escape(f) for f in failures)
    skipped = gate.get("skipped_scanners") or []
    skip_html = ('<p class="muted">未运行的扫描器: %s</p>' % html.escape(", ".join(skipped))) if skipped else ""
    return ("<h2>质量门禁 %s</h2><table><tr><th>检查项</th><th class=\"num\">数量</th></tr>%s</table>%s%s"
            % (b, counts, fail_html, skip_html))


def ai_section(ai):
    if not ai:
        return "<h2>AI 代码审核</h2><p class=\"muted\">未执行。</p>"
    verdict = ai.get("verdict")
    if verdict == "skipped":
        return ("<h2>AI 代码审核 %s</h2><p class=\"muted\">本次跳过：%s</p>"
                % (badge("skipped", "gray"), html.escape(str(ai.get("reason") or ""))))
    kind = "ok" if verdict == "pass" else "bad"
    label = "通过" if verdict == "pass" else "未通过"
    findings = sorted(
        ai.get("findings") or [],
        key=lambda f: SEV_ORDER.get(str(f.get("severity")).lower(), 9),
    )
    rows = "".join(
        "<tr><td>%s</td><td><code>%s:%s</code></td><td>%s</td><td>%s</td></tr>" % (
            badge(f.get("severity") or "info", "sev-" + str(f.get("severity") or "info").lower()),
            html.escape(str(f.get("file") or "-")), html.escape(str(f.get("line", "-"))),
            html.escape(str(f.get("message") or "")),
            html.escape(str(f.get("suggestion") or "-")))
        for f in findings
    ) or '<tr><td colspan="4" class="muted">无发现</td></tr>'
    meta = ai.get("meta") or {}
    notes = []
    if meta.get("unreviewed_chunks"):
        notes.append("%s 个分片未完成审核" % meta["unreviewed_chunks"])
    if meta.get("overflow_files"):
        notes.append("%s 个文件超出分片上限未审核" % len(meta["overflow_files"]))
    if meta.get("truncated_files"):
        notes.append("%s 个文件超长截断" % len(meta["truncated_files"]))
    if meta.get("skipped"):
        notes.append("跳过文件: %s" % "; ".join(
            "%s(%s)" % (s.get("file"), s.get("reason")) for s in meta["skipped"][:8]))
    if meta.get("carried_findings"):
        notes.append("%s 条历史遗留发现未修复（持续跟踪直至代码修复）" % meta["carried_findings"])
    if meta.get("resolved_findings"):
        detail = "; ".join(meta.get("resolved_detail") or []) or "见上次报告"
        notes.append("%s 条历史遗留发现已修复: %s" % (meta["resolved_findings"], detail))
    note_html = ('<p class="muted">%s</p>' % html.escape("；".join(notes))) if notes else ""
    return ("<h2>AI 代码审核 %s</h2><p>%s</p><table><tr><th>严重级别</th><th>位置</th>"
            "<th>问题描述</th><th>修复建议</th></tr>%s</table>%s"
            % (badge(label, kind), html.escape(str(ai.get("summary") or "")), rows, note_html))


def main():
    gate = load("scan/gate.json")
    ai = load("scan/ai.json")
    page = (
        TEMPLATE
        .replace("__NOW__", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        .replace("__GATE_SECTION__", gate_section(gate))
        .replace("__AI_SECTION__", ai_section(ai))
    )
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(page)
    print("report.html generated")


if __name__ == "__main__":
    main()
