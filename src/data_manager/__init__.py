"""微信聊天事实库。

公共入口仅暴露 DataWorker、领域模型和解析器协议；具体 SQLite 实现保持包内聚。
"""

from data_manager.models import (
    IngestResult,
    MessageRecord,
    ParsedMessage,
    ReconcileResult,
    SyncResult,
    ValidationResult,
)
from data_manager.parsers import (
    NullWeChatDatabaseParser,
    PyWxDumpParser,
    WeChatDatabaseParser,
)
from data_manager.worker import DataWorker

__all__ = [
    "DataWorker",
    "IngestResult",
    "MessageRecord",
    "NullWeChatDatabaseParser",
    "ParsedMessage",
    "PyWxDumpParser",
    "ReconcileResult",
    "SyncResult",
    "ValidationResult",
    "WeChatDatabaseParser",
]
