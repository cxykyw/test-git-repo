# AI 代码审查规则（随代码版本走，改动请走 MR）

## 范围
- 只审查 diff 涉及的文件与改动行；忽略 *.lock、target/、build/、dist/、vendor/、自动生成代码、二进制文件

## 一、安全（最高优先级，对应 OWASP Top 10 / CERT Java）
- SQL 注入: 字符串拼接或格式化构造 SQL；MyBatis 使用 ${} 而非 #{}；必须参数化查询
- 反序列化: ObjectInputStream 原生反序列化、fastjson autoType、Jackson enableDefaultTyping、XMLDecoder
- 弱加密与弱随机: MD5/SHA1 存密码、DES/ECB 模式、安全场景（token/验证码/密钥）使用 java.util.Random 而非 SecureRandom
- XXE: XML 解析器（DocumentBuilderFactory/SAXParser）未禁用外部实体与 DTD
- 硬编码: 密钥/密码/token/内网地址写死在代码或仓库配置中
- 敏感信息泄漏: 日志或异常信息输出密码、token、身份证、手机号明文
- 越权与输入: 接口未校验资源归属（IDOR）、文件路径拼接未规范化（穿越风险）、重定向 URL 未白名单校验

## 二、正确性
- NPE: 链式调用与 Map.get 结果直接解引用；equals 应写为常量在前；Optional.get 裸调用
- 比较语义: BigDecimal 用 equals 而非 compareTo；Integer/Long 用 == 比较（缓存区间陷阱）；浮点数直接判等
- 异常处理: catch 后空块或仅打印；finally 中 return；捕获 Exception/Throwable 过宽；重抛时丢失 cause
- 并发: 共享 SimpleDateFormat/HashMap 等非线程安全对象；双重检查锁缺 volatile；无界队列线程池；遍历集合时结构化修改
- 事务: @Transactional 同类自调用或 private 方法失效；事务内执行 RPC/文件 IO；非 RuntimeException 默认不回滚的语义误用
- 资源: 流/连接/Channel 未使用 try-with-resources 关闭；ThreadLocal 未 remove

## 三、性能
- 循环内执行 DB 查询或 RPC（N+1）；循环内拼接字符串应用 StringBuilder
- 集合未预估初始容量；大结果集一次性载入内存；Pattern.compile 在循环内重复编译

## 四、规范（阿里巴巴 Java 开发手册精选）
- 日志: 使用 SLF4J 占位符而非字符串拼接；禁止 System.out/printStackTrace
- POJO: 布尔属性禁止 is 前缀（序列化坑）；包装类比较用 equals；实现 Serializable 需显式 serialVersionUID
- 集合与并发工具: ArrayList/HashMap 初始化指定容量；foreach 中增删元素须用 Iterator
- 禁止魔法值直接出现；公共方法应有 Javadoc

## 严重级别定义
- blocker: 安全漏洞（注入/反序列化/弱加密/硬编码密钥/越权/XXE）或数据损坏
- major: 明确逻辑缺陷（NPE、并发错误、事务失效、异常吞没、资源泄漏）
- minor: 性能问题、规范偏离
- info: 风格与可读性建议
