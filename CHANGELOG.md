# 变更记录

## 2026-09-06

- feat: **累计审核基点** `.ai-review-base`——多次提交攒一次构建审，整段范围审完才推进基点，杜绝漏审；候选链 `.ai-review-base` → `GIT_PREVIOUS_COMMIT` → `HEAD~1`，`git merge-base` 防历史重写
- feat: **结构化输出**——LLM 节点启用 JSON Schema（verdict/severity 枚举、findings 字段锁定、additionalProperties=false）；模型凭据 `structured_output_support=supported` 开启原生 response_format 透传
- feat: **统一人读报告 `report.html`**（make_report.py 渲染），构建产物收敛为 zip + report.html 两项；扫描原始输出迁移至工作区 `scan/` 目录
- feat: **分片并行审核**——diff 按文件切分片（字符预算装箱）、线程池并行送审、结果聚合去重；噪声文件（lock/构建产物/generated/_pb2/min.js/二进制/纯删除）自动跳过并记录原因；单文件行数+字符双上限截断；超分片上限文件标记未审核（blocking 时阻断）
- feat: **按严重级别阻断**——`AI_REVIEW_BLOCK_SEVERITIES`（默认 blocker,critical），major 及以下仅展示；未审完分片/超限文件在 blocking 模式下同样阻断
- fix: LLM 输出 JSON 含未转义控制字符时以 `strict=False` 兜底解析
- fix: 文件读写显式 UTF-8（Jenkins POSIX locale 下中文不炸）；发现徽章 class 与 CSS 样式对齐（.sev-*）；make_report load 失败输出警告
- chore: buildDiscarder 保留 10 次构建/5 份产物；扫描命令输出迁移至 scan/

## 2026-09-05

- feat: **Java 专属审核规则**——LLM 清单四板块（安全/正确性/性能/规范，源自 OWASP Top 10、CERT Java、阿里巴巴 Java 开发手册精选）；Semgrep 扩展 p/security-audit、p/owasp-top-ten 规则集
- feat: **AI 审核层接入 Dify**——薄壳节点 + 规则外置（rules-as-code）+ 应用 API key 本机管理（`~/.config/code-review/dify_api_key`）+ advisory 优先的成熟度路线
- feat: **确定性质量门禁** gate.sh（gitleaks 密钥 / semgrep ERROR / trivy CRITICAL 阻断，报告随构建输出）
- feat: **Jenkins 流水线全链路跑通**——GitHub 手动触发 → 拉代码 → 扫描 → 门禁 → AI 审核 → zip 归档；产物可追溯（文件名含短 SHA + 构建号）
- chore: Jenkins（brew，开机自启）与 semgrep/gitleaks/trivy 安装；Dify 模型凭据配置（火山方舟 ark-code-latest 端点）
