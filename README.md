# test-git-repo

Jenkins 流水线验证仓库（手动触发，不配轮询/webhook）。

## 流水线

拉代码 → 安全扫描（Semgrep / Gitleaks / Trivy 并行）→ 质量门禁（gate.sh）→ AI 审核（ai_review.py，advisory）→ 打包（build.sh）→ 归档产物。

- `gate.sh`：确定性门禁。gitleaks 密钥 / semgrep ERROR 级 / trivy CRITICAL 漏洞任一命中即 FAIL
- `ai_review.py`：取最近一次提交的 diff，按文件切分片（默认 100K 字符/片，`AI_REVIEW_CHUNK_CHARS` 可调）并行送审（`AI_REVIEW_WORKERS`，默认 3），结果聚合去重；lock/构建产物/生成代码自动跳过，单文件超长截断并在结果中标注，超出分片上限的文件同样如实标记。默认 advisory 模式（只展示不阻断）；Jenkins 环境变量设 `DIFY_BLOCKING=1` 后，仅 blocker/critical 级发现或未审完的分片才阻断打包，major 以下只展示（阻断级别可用 `AI_REVIEW_BLOCK_SEVERITIES` 覆盖）
- 规则与契约分层：审查规则在仓库 `ai-review/rules.md`（随代码版本走、改动走 MR）；输出 JSON 契约固定在 Dify 工作流 LLM 节点（稳定接口）。规则可按语言拆分（如 rules-java.md），由 ai_review.py 按 diff 内容选择
- Dify 接入：读取 `~/.config/code-review/dify_api_key`（应用 API 密钥），默认地址 `http://localhost:80`，可用环境变量 `DIFY_URL` 覆盖。工作流定义见 `ai-review/dify-dsl.yml`
- `build.sh`：打包脚本，产出 `app-<commit>-<构建号>.zip`
- 扫描报告与 AI 审核结果（semgrep.json / gitleaks.json / trivy.json / gate-result.json / ai-review-result.json）随构建归档，可在构建页面下载

触发方式：打开 Jenkins 任务页（http://127.0.0.1:8080/job/test-git-repo/），点击 **Build Now**。
