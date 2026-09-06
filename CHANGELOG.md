# 变更记录

## 2026-09-06

- feat: **文件任务模型**——审核单元从"100K 字符分片"重构为固定预算的文件任务（默认 12K 字符）：diff + HEAD 上下文 + semgrep 线索合计为常数上界，**单次调用输入与变更总量无关**；单文件超预算按 hunk 组拆段（单 hunk 再按行切），相邻小文件自动合并；并行 worker 3→6
- feat: **上下文增强**——每个任务附带变更 hunk 在 HEAD 上的周边代码窗口（±40 行、单文件 4K 预算、多 hunk 区间合并、行号即真实行号，要求发现项 line 用真实行号）；新文件回退 diff 新增行
- feat: **semgrep 线索注入**——同一文件的静态扫描命中作为提示词线索附给模型（优先核验真伪但不局限于此），规则扫描与 AI 审核从并行无关联变为接力
- fix: 任务审核"调用+解析"整体自动重试（最多 3 次尝试，带退避）——LLM 链路间歇性流截断（实测同一输入时好时坏，Dify 日志见 node_llm ABORT: Failed to parse structured output）不再一击就把任务判为未完成审核而阻断构建；重试耗尽才落"未完成"并按 blocking 语义拦截
- fix: 分支识别优先取工作区实际签出的 HEAD 分支——Jenkins 注入的 GIT_BRANCH 反映任务 SCM 配置的分支而非构建参数所选分支，原优先级会使按分支隔离失效（首次真实构建中由 AI 审核发现并确认）
- chore: Dify LLM 节点显式 max_tokens=8192，提示词增加输出长度约束（findings ≤20 条、单条 ≤60 字）；已直接改写实例内当前工作流的 draft 与 published 版本（绕过控制台导入会新建应用的问题，改前已 pg_dump 备份），仓库 dify-dsl.yml 与线上保持同步
- feat: **构建分支参数**——Jenkins 构建时传入 `BRANCH` 选择要构建/审核的分支（默认 main，白名单校验防注入，Checkout 阶段按参数切分支）；审核基点与遗留台账按分支独立存储（`.ai-review-base.<分支>` / `.ai-review-state.<分支>.json`），切换分支互不串扰，旧版无后缀 `.ai-review-base` 自动迁移
- fix: 无新增 diff 的空重建不再绕过遗留台账——未修复的阻断级发现继续阻断
- feat: **按严重级别阻断启用**——Jenkinsfile 设 `DIFY_BLOCKING=1`，blocker/critical 级发现（`AI_REVIEW_BLOCK_SEVERITIES` 可调）、未审完分片、超限未审文件使构建失败，打包阶段不执行
- feat: **遗留发现台账** `.ai-review-state.<分支>.json`——阻断级发现落账（含被标记代码 3 行指纹），每次构建对照 HEAD 自动判定：代码仍在 = 未修复（并入报告、持续阻断，标注首次发现构建号），代码已变/文件删除 = 已修复（出账并在报告标注）。问题修复与否由代码内容判定，不依赖对旧提交的重复扫描；台账与审核基点解耦，新提交照常增量审
- fix: LLM 输出为合法 JSON 但非对象（裸数组/字符串/数字）时优雅降级为该分片未完成审核，不再因 AttributeError 使整轮审核静默跳过；JSON 解析失败的失败原因带上具体异常（行列号）
- fix: 二进制文件判定改为仅匹配 diff 头部元信息行——此前 ai_review.py 自身进入 diff 时（源码含判定用字面量）会被误判为二进制文件而跳过漏审
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
