# test-git-repo

Jenkins 流水线验证仓库（手动触发，不配轮询/webhook）。

- `Jenkinsfile`：流水线定义（拉代码 → build.sh 打包 → 归档 zip 产物）
- `build.sh`：打包脚本，产出 `app-<commit>-<构建号>.zip`

触发方式：打开 Jenkins 任务页，点击 **Build Now**。
