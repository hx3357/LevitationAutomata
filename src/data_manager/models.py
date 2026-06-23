"""DataManager 领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ParsedMessage:
    source_type: str
    source_message_id: Optional[str]
    sent_at: str
    sent_at_source: str
    observed_at: str
    chat_id: str
    chat_type: str
    sender_id: Optional[str]
    sender_name: str
    sender_remark: Optional[str]
    direction: str
    message_type: str
    content: Optional[str]
    file_path: Optional[str]
    file_status: str
    mentioned_agent: bool
    raw_json: str


@dataclass(frozen=True)
class MessageRecord:
    id: int
    source_message_id: Optional[str]
    sent_at: str
    sent_at_source: str
    observed_at: str
    chat_id: str
    chat_type: str
    sender_id: Optional[str]
    sender_name: str
    sender_remark: Optional[str]
    direction: str
    message_type: str
    content: Optional[str]
    file_path: Optional[str]
    file_status: str
    mentioned_agent: bool
    ingest_source: str
    reconcile_status: str
    raw_json: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "MessageRecord":
        return cls(
            id=int(row["id"]),
            source_message_id=row["source_message_id"],
            sent_at=str(row["sent_at"]),
            sent_at_source=str(row["sent_at_source"]),
            observed_at=str(row["observed_at"]),
            chat_id=str(row["chat_id"]),
            chat_type=str(row["chat_type"]),
            sender_id=row["sender_id"],
            sender_name=str(row["sender_name"]),
            sender_remark=row["sender_remark"],
            direction=str(row["direction"]),
            message_type=str(row["message_type"]),
            content=row["content"],
            file_path=row["file_path"],
            file_status=str(row["file_status"]),
            mentioned_agent=bool(row["mentioned_agent"]),
            ingest_source=str(row["ingest_source"]),
            reconcile_status=str(row["reconcile_status"]),
            raw_json=str(row["raw_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True)
class IngestResult:
    message_id: int
    inserted: bool


@dataclass(frozen=True)
class SyncResult:
    status: str
    message_count: int
    error: Optional[str] = None


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    online_count: int = 0
    database_count: int = 0
    matched_count: int = 0
    online_only_count: int = 0
    database_only_count: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: Optional[str] = None
