# test-git-repo

Jenkins 流水线验证仓库（手动触发，不配轮询/webhook）。

## 流水线

拉代码 → 安全扫描（Semgrep / Gitleaks / Trivy 并行）→ 质量门禁（gate.sh）→ AI 审核（ai_review.py，advisory）→ 打包（build.sh）→ 归档产物。

- `gate.sh`：确定性门禁。gitleaks 密钥 / semgrep ERROR 级 / trivy CRITICAL 漏洞任一命中即 FAIL
- `ai_review.py`：取最近一次提交的 diff（超过 2000 行截断），连同 `ai-review/rules.md` 发给 Dify 工作流 `ai-code-review`，解析 JSON 结论。默认 advisory 模式（结果只展示不阻断）；在 Jenkins 环境变量里设 `DIFY_BLOCKING=1` 后，verdict=fail 会阻断打包
- Dify 接入：读取 `~/.config/code-review/dify_api_key`（应用 API 密钥），默认地址 `http://localhost:80`，可用环境变量 `DIFY_URL` 覆盖。工作流定义见 `ai-review/dify-dsl.yml`
- `build.sh`：打包脚本，产出 `app-<commit>-<构建号>.zip`
- 扫描报告与 AI 审核结果（semgrep.json / gitleaks.json / trivy.json / gate-result.json / ai-review-result.json）随构建归档，可在构建页面下载

触发方式：打开 Jenkins 任务页（http://127.0.0.1:8080/job/test-git-repo/），点击 **Build Now**。
