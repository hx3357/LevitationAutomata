# wx DataManager MVP 实施计划

## 1. MVP 目标与验收标准

实现一个基于 SQLite 的消息数据层：

- wxauto 捕获的消息在进入过滤器前可靠落库；
- 重复消息不会产生重复记录；
- gateway 重启、反复启动或写入中断后，已有数据仍可查询；
- 启动时调用微信数据库解析器策略；MVP 空解析器返回 0 条并输出 warning；
- 支持按聊天、发送人和带时区时间范围查询；
- 实现默认关闭的时间窗对账流程；
- 对账失败或解析结果异常时不修改已有消息。

MVP 不要求真正解析微信数据库，但解析器接口、调度、批次记录、校验和对账算法必须可以被真实实现直接替换使用。

## 2. 配置与数据模型

### 配置

在 `WxAutoConfig` 中增加：

```yaml
data_manager:
  enabled: true
  database_path: data/messages.db
  timezone: Asia/Shanghai
  agent_names:
    - Levi
  reconciliation:
    enabled: false
    interval_seconds: 600
    overlap_seconds: 120
    settle_delay_seconds: 30
    time_tolerance_seconds: 5
```

规则：

- 相对数据库路径以插件根目录为基准。
- 无效 IANA 时区视为配置错误并回退 `Asia/Shanghai`，同时输出 warning。
- 对账默认关闭。
- `data_manager.enabled=false` 时 adapter 保持现有行为。

### 数据库

创建 `messages`、`message_sources`、`sync_runs`、`reconcile_runs` 和
`message_reconciliations` 表。`message_sources` 使用
`(source_type, source_message_id)` 条件唯一索引承担来源内幂等，避免对账将
`ingest_source` 改为 `reconciled` 后失去去重能力。首次打开数据库时设置：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

使用 `PRAGMA user_version` 管理 schema 版本。MVP schema 为版本 1。

## 3. 实现步骤

### 阶段 A：领域模型和存储层

1. 新增统一消息类型：
   - `MessageRecord`
   - `ParsedMessage`
   - `IngestResult`
   - `SyncResult`
   - `ReconcileResult`
2. 实现时间工具：
   - 读取 IANA 时区；
   - 将无时区 API timestamp 解释为配置时区；
   - 输出带偏移量 ISO 8601；
   - 实时时间同时填入 `sent_at` 和 `observed_at`，并设置 `sent_at_source=observed_fallback`。
3. 实现消息归一化：
   - `friend/self` 转换为方向；
   - 根据内容和文件扩展名识别消息类型；
   - 设置 `file_path`、`file_status`；
   - 根据 `agent_names` 识别群聊 `mentioned_agent`；
   - 保存 `sender_remark` 和原始 JSON。
4. 实现 SQLite repository：
   - schema 初始化和迁移；
   - 通过 `message_sources` 进行来源内幂等写入；
   - 批量事务；
   - 消息范围查询；
   - 同步和对账批次状态记录。

### 阶段 B：异步集成

1. 新增独立 `DataWorker`，使用单线程串行执行 SQLite 操作，避免阻塞 asyncio 事件循环。
2. adapter `connect()` 顺序：
   - 启动 `DataWorker`；
   - 初始化 schema；
   - 调用启动同步；
   - 启动微信 worker 和聊天监听；
   - 对账开启时启动定时任务。
3. `_fetch_updates()` 生成 raw 消息后，在执行 filter 前调用 `ingest_online()`。
4. 数据落库失败时记录 error，但单条数据库失败不终止微信轮询；后续离线同步负责补账。
5. `disconnect()` 顺序：
   - 停止接收新消息；
   - 对账开启时尽力执行关闭前对账；
   - 取消对账任务；
   - 刷新并停止 `DataWorker`。

### 阶段 C：解析器策略

1. 定义 `WeChatDatabaseParser` Protocol 和解析器注册/选择接口。
2. 实现 `NullWeChatDatabaseParser`：
   - `validate_source()` 返回“未配置真实解析器”；
   - `iter_messages()` 返回空集合；
   - 每次同步输出 warning；
   - `sync_runs.status=no_parser`。
3. DataManager 只接收 `ParsedMessage`，不得引用具体微信数据库表或字段。

### 阶段 D：默认关闭的对账

1. 实现安全窗口计算、2 分钟重叠和 30 秒稳定延迟。
2. 对数据库解析结果执行整批合法性检查；失败时事务回滚。
3. 对两侧消息按聊天和发送人分组，以时间优先进行一对一匹配：
   - 默认 ±5 秒；
   - 内容和类型必须兼容；
   - 重复内容按时间顺序配对；
   - 歧义候选不自动合并。
4. 匹配成功后以数据库字段为准，保留有效在线文件路径和双方原始数据。
   两个来源记录绑定到同一 canonical message，来源 ID 分别保存在
   `message_sources`。
5. 未匹配消息分别标记 `online_only`、`database_only`。
6. 只有 `reconciliation.enabled=true` 才注册定时任务和执行启动/关闭对账。

## 4. 测试计划（暂不测试）

新增 `tests/`，至少覆盖：

- 文本、图片路径、缺失图片、动画表情、语音、视频、音乐和链接的归一化；
- `friend/self` 方向转换及 `sender_id=NULL`；
- `Asia/Shanghai` 时间解析，输出包含 `+08:00`；
- 群聊精确 `@Levi` 命中，普通文本和私聊不误判；
- 相同来源消息重复写入只保留一条；
- 相同时间消息通过本地 ID 稳定排序；
- 空解析器产生 warning、0 条结果和 `no_parser` 批次状态；
- 默认配置不启动或执行对账；
- ±5 秒内同发送人、同内容消息正确匹配；
- 连续发送相同内容时按时间一对一匹配；
- 匹配成功后数据库时间和 sender ID 覆盖，在线有效文件路径保留；
- 解析结果乱码、越界或异常重复时整批回滚；
- 模拟事务中断后数据库无半批提交；
- `compileall` 和 `pyright` 通过。

## 5. 手工验收

1. 启动 gateway，确认创建 SQLite 文件和 schema。
2. 发送文本、图片和动画表情，确认在过滤前写入正确字段。
3. 重启 gateway，再次返回相同 API 消息时确认没有重复记录。
4. 确认日志包含空微信数据库解析器 warning，但 gateway 正常工作。
5. 确认默认配置下没有对账任务和历史数据改写。
6. 在测试配置中开启对账，注入模拟解析器，验证匹配、补充、异常回滚和批次统计。

## 6. 已确认默认值

- 时区：`Asia/Shanghai`。
- 时间格式：带 UTC 偏移量的 ISO 8601。
- 实时消息无法取得真实发送时间时，`sent_at=observed_at`。
- 微信数据库字段优先级高于实时捕获字段。
- 跨来源不比较 `source_message_id`。
- 对账采用时间优先、内容和身份约束的一对一匹配。
- 对账默认关闭；空解析器只警告，不阻止启动。
