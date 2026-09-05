# AI 代码审查规则

## 范围
- 只审查 diff 涉及的文件与改动行，不评价未改动代码
- 忽略: *.lock、dist/、vendor/、自动生成文件、二进制文件

## 审查维度
- 安全: SQL/命令注入、XSS、越权访问、硬编码密钥、不安全的反序列化
- 正确性: 空引用、边界条件、异常被静默吞掉、并发与事务问题
- 资源: 连接/文件句柄未关闭、循环内重复创建重量级对象
- 兼容性: 公共接口签名变更、配置格式不兼容改动

## 严重级别
- blocker: 可能导致安全漏洞或数据损坏
- major: 明确的逻辑缺陷，应当修复
- minor: 建议修复
- info: 风格建议

## 输出要求
严格输出 JSON，不要输出任何其他文字：
{"verdict": "pass|fail", "summary": "一句话总结", "findings": [{"file": "路径", "line": 行号, "severity": "blocker|major|minor|info", "message": "问题描述", "suggestion": "修复建议"}]}
verdict 判定: 存在 blocker 或 major 即 fail；否则 pass。没有发现时 findings 为空数组。
