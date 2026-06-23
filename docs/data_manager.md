# wx 数据模块

## 目标

数据模块为 Levi Agent 提供独立于微信客户端的聊天事实库，负责：

- 实时记录 wxauto/UIAutomation 捕获的消息；
- 在启动或定时任务中调用微信数据库解析器，补充离线消息；
- 提供按聊天、发送人和时间范围查询消息的接口；
- 在启用对账后，以微信数据库记录为高置信度来源，校正实时捕获结果；
- 为后续 Honcho 等记忆模块提供稳定、可重建的数据源。

数据模块与记忆模块分离。SQLite 保存原始聊天事实，记忆模块只消费数据模块提供的消息，不直接承担同步、去重或对账职责。

## 设计约束

- 支持 gateway 间断运行、重复启动和同步过程中被强制终止。
- wxauto/UIAutomation 和微信数据库的消息 ID 不同，不能通过消息 ID 跨来源关联。
- 微信数据库字段和表结构暂不确定，MVP 只定义解析器策略接口，并提供返回空结果的占位实现。
- 占位解析器被调用时必须输出 warning，明确说明离线补账未实际执行。
- 微信数据库是高置信度来源；实时捕获是低置信度、低延迟来源。
- 对账功能必须实现，但默认关闭。关闭时不得删除、替换或批量修改已有消息。
- 不使用磁盘 JSON 维护同步状态；消息、运行状态、游标和对账批次统一保存在 SQLite。

## 时间约定

配置增加 IANA 时区字段：

```yaml
data_manager:
  timezone: Asia/Shanghai
```

默认时区为 `Asia/Shanghai`（UTC+08:00）。`sent_at`、`observed_at` 和其他业务时间统一存储为带 UTC 偏移量的 ISO 8601 字符串：

```text
2026-05-17T08:32:08+08:00
```

- `sent_at`：消息发送时间。实时消息无法取得真实发送时间时，暂时使用捕获时间，`sent_at_source` 记为 `observed_fallback`；微信数据库补全后改为 `wechat_database`。
- `observed_at`：本程序首次观察到该消息的时间。
- SQLite 内不另存时区列；偏移量已包含在时间字符串中，配置中的 IANA 时区用于解析无时区时间和处理夏令时。
- 所有范围查询先转换为同一时区后比较。实现时同时保存可排序的 UTC epoch 毫秒字段属于 MVP 后优化项。

## 消息模型

### MVP 必需字段

| 字段 | SQLite 类型 | 约束/含义 |
|---|---|---|
| `id` | INTEGER | 本地主键，自增 |
| `source_message_id` | TEXT | 来源内消息 ID；允许为空，仅保证同一来源内去重 |
| `sent_at` | TEXT | 带偏移量的 ISO 8601 消息时间 |
| `sent_at_source` | TEXT | `observed_fallback` / `wechat_database` |
| `observed_at` | TEXT | 带偏移量的 ISO 8601 首次观察时间 |
| `chat_id` | TEXT | 群聊名或私聊对象名 |
| `chat_type` | TEXT | `group` / `dm` |
| `sender_id` | TEXT | nullable；未来由微信数据库提供稳定 wxid |
| `sender_name` | TEXT | UIAutomation 提供的显示名 |
| `sender_remark` | TEXT | nullable；API 提供时保存 |
| `direction` | TEXT | `incoming` / `outgoing` |
| `message_type` | TEXT | 归一化消息类型 |
| `content` | TEXT | 文本正文、媒体占位符或原始可见内容 |
| `file_path` | TEXT | nullable；捕获文件的本地路径 |
| `file_status` | TEXT | `not_applicable` / `available` / `missing` / `capture_failed` |
| `mentioned_agent` | INTEGER | SQLite boolean，`0` / `1` |
| `ingest_source` | TEXT | `wxauto_online` / `wechat_database` / `reconciled` |
| `reconcile_status` | TEXT | `unreconciled` / `matched` / `online_only` / `database_only` |
| `raw_json` | TEXT | 原始来源记录的 JSON |
| `created_at` | TEXT | 首次落库时间 |
| `updated_at` | TEXT | 最后更新时间 |

`message_type` 的 MVP 枚举：

```text
text
file
image
animated_emoji
voice
video
music
link
unknown
```

### 来源 ID 与唯一约束

两个来源的消息 ID 不可比较，而且消息对账后 `ingest_source` 会变成
`reconciled`，因此不能用 `messages.ingest_source` 作为永久去重键。
`messages.source_message_id` 只保存该 canonical message 的首个来源 ID，真正的
来源内幂等由 `message_sources` 表保证：

```sql
CREATE UNIQUE INDEX uq_message_sources_source_id
ON message_sources(source_type, source_message_id)
WHERE source_message_id IS NOT NULL;
```

其中 `source_type` 只能是 `wxauto_online` 或 `wechat_database`。重复收到同一来源
消息时，通过该表找到已有 canonical message；对账成功后，把另一来源的 ID 追加
到同一 canonical message，而不是丢失或覆盖原 ID。

### 消息类型与文件状态归一化

- `content` 是存在的本地路径：
  - 图片扩展名 → `image`
  - 音频扩展名 → `voice`
  - 视频扩展名 → `video`
  - 其他扩展名 → `file`
  - `file_status=available`
- `content` 看似路径但文件不存在：
  - 根据扩展名决定类型；
  - `file_path` 保留原值；
  - `file_status=missing`
- `[动画表情]` → `animated_emoji`
- `[图片]` → `image`
- `[语音]` → `voice`
- `[视频]` → `video`
- `[音乐]` → `music`
- `[链接]` → `link`
- 其他内容 → `text`
- 无需文件的类型使用 `file_status=not_applicable`；应当捕获文件但只有占位符时使用 `capture_failed`。

### Agent 提及识别

配置增加 Agent 名称列表：

```yaml
data_manager:
  agent_names:
    - Levi
    - 小Levi
```

群聊正文包含规范化后的 `@名称` 时，`mentioned_agent=1`。匹配时忽略 `@` 后微信可能插入的普通空格或窄空格，但不进行模糊昵称匹配。私聊默认不视为 mention。微信数据库解析器未来若能提供结构化提及信息，以数据库结果覆盖文本推断结果。

## SQLite 表

MVP 至少包含以下表：

### `messages`

保存统一消息模型。推荐索引：

```sql
CREATE INDEX idx_messages_chat_sent
ON messages(chat_id, sent_at, id);

CREATE INDEX idx_messages_sender_sent
ON messages(sender_name, sent_at, id);
```

时间相同时使用本地 `id` 保证稳定排序。

### `message_sources`

保存 canonical message 对应的原始来源：

- `id`
- `message_id`：外键指向 `messages.id`
- `source_type`：`wxauto_online` / `wechat_database`
- `source_message_id`：nullable
- `raw_json`
- `created_at`

`(source_type, source_message_id)` 在 ID 非空时唯一。没有来源 ID 的消息仍可写入，
但只能依靠对账合并，不能保证重放幂等。

### `sync_runs`

记录启动补账和定时同步执行情况：

- `id`
- `sync_type`：`startup` / `scheduled` / `shutdown`
- `started_at`
- `finished_at`
- `status`：`running` / `succeeded` / `failed` / `no_parser`
- `message_count`
- `error`

运行时间仅用于诊断，不作为消息是否已同步的正确性依据。

### `reconcile_runs`

记录每个对账批次：

- `id`
- `window_start`
- `window_end`
- `started_at`
- `finished_at`
- `status`
- `online_count`
- `database_count`
- `matched_count`
- `online_only_count`
- `database_only_count`
- `error`

### `message_reconciliations`

保存来源消息之间的关联和匹配证据：

- `id`
- `canonical_message_id`
- `online_message_id`
- `database_message_id`
- `match_score`
- `time_delta_ms`
- `match_method`
- `created_at`

## 数据流

### 实时消息

实时消息应在过滤器之前持久化：

```text
GetListenMessage
    ├── DataManager.ingest_online()
    └── filter pipeline
            └── Hermes Agent
```

黑名单消息、命令、自发消息可以不进入 Agent，但仍可作为完整聊天事实保存。实时写入使用 SQLite 事务和来源内唯一约束保证 gateway 重启后幂等。

### 启动补账

1. 初始化 SQLite schema。
2. 创建 `startup` 类型的 `sync_run`。
3. 调用配置选中的微信数据库解析器。
4. 解析器返回消息时，按来源内 ID 幂等写入 `wechat_database` 记录。
5. 解析器为空实现时：
   - 输出 warning；
   - 将 `sync_run.status` 记为 `no_parser`；
   - 返回 0 条消息；
   - 不阻止 gateway 启动。
6. 如果启用对账，对已导入的安全时间窗执行对账；默认配置下跳过。

## 微信数据库解析器策略

解析器接口不依赖具体微信版本和数据库 schema：

```python
class WeChatDatabaseParser(Protocol):
    @property
    def name(self) -> str: ...

    def validate_source(self) -> ValidationResult: ...

    def iter_messages(
        self,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> Iterable[ParsedMessage]: ...
```

MVP 提供：

```python
class NullWeChatDatabaseParser:
    """占位解析器：输出 warning，并返回空消息集合。"""
```

后续按微信版本增加实现，例如 `WeChat39DatabaseParser`。解析器负责把微信原始字段转换成统一 `ParsedMessage`，DataManager 不直接了解加密数据库的表名和字段。

## 对账策略

### 配置与触发

对账代码在 MVP 中实现，但默认关闭：

```yaml
data_manager:
  reconciliation:
    enabled: false
    interval_seconds: 600
    overlap_seconds: 120
    settle_delay_seconds: 30
    time_tolerance_seconds: 5
```

启用后：

- gateway 启动完成离线导入后执行一次；
- 每 10 分钟执行一次；
- 正常关闭前尽力执行一次，但失败不得阻塞关闭；
- 扫描窗口为“上次成功窗口终点前 2 分钟”到“当前时间前 30 秒”；
- 若没有上次成功记录，则从当前 gateway 启动时间开始。

### 批次合法性检查

数据库解析结果必须先通过整批校验：

- 解析器源文件验证成功；
- 消息时间全部落在请求窗口或允许的边界误差内；
- 必需字段可转换；
- 时间排序和数量处于合理范围；
- 内容不是整批为空或明显乱码；
- 不出现异常比例的重复记录。

任一校验失败时整批回滚，记录 warning 和失败的 `reconcile_run`，不修改现有消息。

### 匹配

匹配候选必须满足：

- `chat_id` 相同；
- `sender_id` 均存在时优先要求相同，否则比较 `sender_name`；
- 归一化消息类型兼容；
- 归一化内容相同或媒体占位符兼容；
- 时间差默认不超过 ±5 秒。

排序和消歧规则：

1. 优先最小时间差。
2. 其次优先 `sender_id` 相同。
3. 其次优先正文完全相同。
4. 相同内容连续发送时，两侧分别按时间排序，执行一对一匹配。
5. 无法唯一确定时不匹配，保留双方并记录异常。

内容归一化仅处理换行、首尾空白、微信提及空格和已知媒体占位符，不做模糊语义匹配。

### 合并结果

- 匹配成功：
  - 以微信数据库的 `sent_at`、`sender_id`、结构化类型和提及信息覆盖实时推断值；
  - 保留在线捕获到且仍有效的 `file_path`；
  - `ingest_source=reconciled`；
  - `reconcile_status=matched`；
  - 保留双方原始 JSON 和来源 ID；
  - 写入 `message_reconciliations`。
- 仅在线存在：保留原记录，标记 `online_only`。
- 仅数据库存在：作为新消息保留，标记 `database_only`。
- 不直接按时间窗删除并重建消息，避免解析失败或匹配歧义造成数据丢失。

整个批次在单个事务中提交。gateway 在事务提交前被终止时，SQLite 自动回滚；下次通过重叠窗口重新执行。

## 查询接口

MVP 提供 Python 内部接口：

```python
ingest_online(raw_message) -> IngestResult
sync_from_database(start_at=None, end_at=None) -> SyncResult
reconcile(start_at, end_at) -> ReconcileResult
query_messages(
    chat_id,
    start_at=None,
    end_at=None,
    sender_id=None,
    sender_name=None,
    message_types=None,
    limit=200,
) -> list[MessageRecord]
```

查询默认按 `(sent_at, id)` 升序返回。MVP 暂不提供 Agent tool；记忆工具在数据层稳定后单独接入。

## MVP 范围外

- 真实微信数据库解密及字段映射实现；
- Honcho 投递和长期人物画像；
- OCR、语音转录、图片理解；
- 跨昵称变化自动合并身份；
- 消息撤回、编辑和删除同步；
- FTS5 全文检索；
- 多微信账号支持。
