# test-git-repo

Jenkins 流水线验证仓库（手动触发，不配轮询/webhook）。

## 流水线

拉代码 → 安全扫描（Semgrep / Gitleaks / Trivy 并行）→ 质量门禁（gate.sh）→ 打包（build.sh）→ 归档产物。

- `gate.sh`：确定性门禁。gitleaks 密钥 / semgrep ERROR 级 / trivy CRITICAL 漏洞任一命中即 FAIL
- `build.sh`：打包脚本，产出 `app-<commit>-<构建号>.zip`
- 扫描报告（semgrep.json / gitleaks.json / trivy.json / gate-result.json）随构建归档，可在构建页面下载

触发方式：打开 Jenkins 任务页（http://127.0.0.1:8080/job/test-git-repo/），点击 **Build Now**。
