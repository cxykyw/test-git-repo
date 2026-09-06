# test-git-repo — AI DevSecOps 流水线验证仓库

Jenkins + Dify 的 AI 代码审核流水线验证项目。手动触发，无轮询/webhook；
全部运行在本机（M1 Pro/Mac），零新增服务器。

## 流水线全景

```text
GitHub → Jenkins 手动构建（Build with Parameters 传入 BRANCH 选择分支，默认 main）
  → Checkout（按参数切分支）
  → 安全扫描（Semgrep×3 规则集 / Gitleaks / Trivy 并行 → scan/*.json）
  → 确定性质量门禁（gate.sh → scan/gate.json）
  → Dify AI 分片并行审核（ai_review.py → scan/ai.json）
  → make_report.py 渲染 report.html
  → build.sh 打包 zip
  → 归档：zip + report.html（仅此两项）
```

## 构建产物

- `app-<短SHA>-<构建号>.zip`：最终交付包
- `report.html`：人读审核报告（门禁结论 + AI 发现表格 + 跳过/截断/未审明细），浏览器直接打开
- 机器可读中间数据在构建工作区 `scan/` 目录，不归档

## 文件说明

| 文件 | 职责 |
|---|---|
| `Jenkinsfile` | 流水线定义；buildDiscarder 只保留 10 次构建/5 份产物 |
| `gate.sh` | 确定性门禁：gitleaks 密钥 / semgrep ERROR / trivy CRITICAL 任一命中即 FAIL |
| `ai_review.py` | AI 审核：累计基点取 diff → 按文件分片并行送审 → 聚合去重 → 遗留台账核对修复状态 → 写 scan/ai.json |
| `make_report.py` | 把 scan/gate.json + scan/ai.json 渲染成 report.html（post always 阶段执行，门禁失败也有报告） |
| `build.sh` | 打包最终 zip |
| `ai-review/rules.md` | 审查规则（规则即代码：随代码版本走，改动走 MR） |
| `ai-review/dify-dsl.yml` | Dify 工作流定义（可导入重建；与线上保持同步） |

## Dify 工作流（ai-code-review）

- 入口：http://localhost:80（本机 Docker 实例）；模型 Doubao-Seed-2.1-turbo
  （openai_api_compatible 接入火山方舟 ark-code-latest 端点；模型凭据
  structured_output_support=supported，schema 经 response_format 原生透传）
- 图：start（diff 必填 + rules 可选）→ llm（薄壳提示词：固定输出契约 + {{#node_start.rules#}}）→ end（review）
- LLM 节点开启结构化输出（JSON Schema：verdict/severity 枚举、findings 字段锁定）
- 分层原则：「输出契约在节点，审查策略在仓库」

## AI 审核语义

- **advisory（脚本默认）**：结果只展示不阻断
- **DIFY_BLOCKING=1（Jenkinsfile 已启用）**：仅以下情况阻断打包——
  AI_REVIEW_BLOCK_SEVERITIES（默认 blocker,critical）级发现、未审完的分片、
  超出分片上限未审核的文件；major 及以下仅展示
- **遗留台账 `.ai-review-state.<分支>.json`（工作区状态，不入库）**：阻断级发现落账，
  每次构建对照 HEAD 代码逐一核对——被标记代码仍在 = 未修复（并入报告、
  继续阻断），已变更或文件删除 = 已修复（出账并在报告标注）。
  问题修复与否由代码内容判定，不依赖对旧提交的重复扫描；无新增 diff 的
  空重建同样核对台账，不修复不放行；台账按分支独立
- AI 基础设施故障（key 缺失 / Dify 不可达 / 输出不合规）：跳过本次审核，构建不红
- 确定性门禁 FAIL：AI 审核与打包不执行

## 大 diff 与累计审核基点

- diff 按文件切分片（默认 100K 字符/片）并行送审，结果聚合去重
- 自动跳过并记录：lock/构建产物目录/generated 目录/_pb2/.min.js/二进制/纯删除文件
- 单文件超 800 行或分片字符预算时截断并标注；超出分片上限的文件标记为未审核
- 基点与台账按分支独立：`.ai-review-base.<分支>` 记录该分支上次完整审核的 HEAD，
  多次提交攒一次构建审、不漏审；只有整段范围审完才推进基点，否则下次从旧基点
  重审；旧版无后缀 `.ai-review-base` 在分支首次构建时自动迁移
- 分支由构建参数 `BRANCH` 传入（Jenkinsfile Checkout 阶段按参数切分支）

## 可调参数（Jenkins 环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| DIFY_BLOCKING | 脚本未设；Jenkinsfile 已设 1 | 设为 1 开启 AI 阻断 |
| AI_REVIEW_BLOCK_SEVERITIES | blocker,critical | 阻断级严重级别 |
| AI_REVIEW_CHUNK_CHARS | 100000 | 单分片字符预算 |
| AI_REVIEW_FILE_LINES | 800 | 单文件 diff 行数上限 |
| AI_REVIEW_MAX_CHUNKS | 40 | 分片上限，超出部分标记未审核 |
| AI_REVIEW_WORKERS | 3 | 并行审核线程数 |
| DIFY_URL | http://localhost:80 | Dify 地址 |

## 本机依赖

- Jenkins（brew services，http://127.0.0.1:8080，任务 test-git-repo）
- Dify（本机 Docker 实例）；应用 API key 存于 `~/.config/code-review/dify_api_key`
- 扫描工具：/opt/homebrew/bin 下的 semgrep / gitleaks / trivy

## 已知事项

- Dify 实例密码登录报 Invalid encrypted data（实例密钥问题，浏览器会话不受影响）
- semgrep 命令带 `|| true`：PoC 阶段扫描器不弄红构建；接真实仓库后应移除
- 遗留台账按"被标记代码 3 行指纹是否仍在 HEAD"判定修复；改动被标记行所在
  上下文会判定为已修复出账，改动本身会进入下次 diff 被重新审核

变更历史见 [CHANGELOG.md](CHANGELOG.md)。
