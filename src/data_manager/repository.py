"""DataManager 的 SQLite 持久化实现。"""

from __future__ import annotations

import sqlite3
import re
from collections.abc import Sequence
from datetime import tzinfo
from pathlib import Path
from typing import Optional

from data_manager.models import (
    IngestResult,
    MessageRecord,
    ParsedMessage,
    ReconcileResult,
)
from data_manager.time_utils import normalize_datetime, now_iso

_SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id TEXT,
    sent_at TEXT NOT NULL,
    sent_at_source TEXT NOT NULL
        CHECK (sent_at_source IN ('observed_fallback', 'wechat_database')),
    observed_at TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL CHECK (chat_type IN ('group', 'dm')),
    sender_id TEXT,
    sender_name TEXT NOT NULL,
    sender_remark TEXT,
    direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    message_type TEXT NOT NULL CHECK (message_type IN (
        'text', 'file', 'image', 'animated_emoji', 'voice',
        'video', 'music', 'link', 'unknown'
    )),
    content TEXT,
    file_path TEXT,
    file_status TEXT NOT NULL CHECK (file_status IN (
        'not_applicable', 'available', 'missing', 'capture_failed'
    )),
    mentioned_agent INTEGER NOT NULL CHECK (mentioned_agent IN (0, 1)),
    ingest_source TEXT NOT NULL CHECK (ingest_source IN (
        'wxauto_online', 'wechat_database', 'reconciled'
    )),
    reconcile_status TEXT NOT NULL CHECK (reconcile_status IN (
        'unreconciled', 'matched', 'online_only', 'database_only'
    )),
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'wxauto_online', 'wechat_database'
    )),
    source_message_id TEXT,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_message_sources_source_id
ON message_sources(source_type, source_message_id)
WHERE source_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_message_sources_message
ON message_sources(message_id);

CREATE INDEX IF NOT EXISTS idx_messages_chat_sent
ON messages(chat_id, sent_at, id);

CREATE INDEX IF NOT EXISTS idx_messages_sender_sent
ON messages(sender_name, sent_at, id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type TEXT NOT NULL CHECK (sync_type IN ('startup', 'scheduled', 'shutdown')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'running', 'succeeded', 'failed', 'no_parser'
    )),
    message_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS reconcile_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    online_count INTEGER NOT NULL DEFAULT 0,
    database_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    online_only_count INTEGER NOT NULL DEFAULT 0,
    database_only_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS message_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    online_message_id INTEGER NOT NULL,
    database_message_id INTEGER NOT NULL,
    match_score REAL NOT NULL,
    time_delta_ms INTEGER NOT NULL,
    match_method TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class SQLiteMessageRepository:
    def __init__(self, database_path: Path, timezone: tzinfo) -> None:
        self._database_path = database_path
        self._timezone = timezone
        self._connection: Optional[sqlite3.Connection] = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLite repository 尚未打开")
        return self._connection

    def open(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            connection.close()
            raise RuntimeError(
                f"数据库 schema 版本 {version} 高于当前支持版本 {_SCHEMA_VERSION}"
            )
        if version < 1:
            connection.executescript(_SCHEMA_V1)
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._connection = connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def ingest(self, message: ParsedMessage) -> IngestResult:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = self._ingest_with_connection(connection, message)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

    def _ingest_with_connection(
        self,
        connection: sqlite3.Connection,
        message: ParsedMessage,
    ) -> IngestResult:
        if message.source_message_id is not None:
            existing = connection.execute(
                """
                SELECT message_id
                FROM message_sources
                WHERE source_type = ? AND source_message_id = ?
                """,
                (message.source_type, message.source_message_id),
            ).fetchone()
            if existing is not None:
                return IngestResult(int(existing["message_id"]), False)

        current = now_iso(self._timezone)
        reconcile_status = (
            "database_only"
            if message.source_type == "wechat_database"
            else "unreconciled"
        )
        cursor = connection.execute(
            """
            INSERT INTO messages (
                source_message_id, sent_at, sent_at_source, observed_at,
                chat_id, chat_type, sender_id, sender_name, sender_remark,
                direction, message_type, content, file_path, file_status,
                mentioned_agent, ingest_source, reconcile_status, raw_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.source_message_id,
                message.sent_at,
                message.sent_at_source,
                message.observed_at,
                message.chat_id,
                message.chat_type,
                message.sender_id,
                message.sender_name,
                message.sender_remark,
                message.direction,
                message.message_type,
                message.content,
                message.file_path,
                message.file_status,
                int(message.mentioned_agent),
                message.source_type,
                reconcile_status,
                message.raw_json,
                current,
                current,
            ),
        )
        message_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO message_sources (
                message_id, source_type, source_message_id, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                message_id,
                message.source_type,
                message.source_message_id,
                message.raw_json,
                current,
            ),
        )
        return IngestResult(message_id, True)

    def start_sync_run(self, sync_type: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO sync_runs (sync_type, started_at, status)
            VALUES (?, ?, 'running')
            """,
            (sync_type, now_iso(self._timezone)),
        )
        return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        message_count: int,
        error: Optional[str] = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE sync_runs
            SET finished_at = ?, status = ?, message_count = ?, error = ?
            WHERE id = ?
            """,
            (now_iso(self._timezone), status, message_count, error, run_id),
        )

    def query_messages(
        self,
        chat_id: str,
        *,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_name: Optional[str] = None,
        message_types: Optional[Sequence[str]] = None,
        limit: int = 200,
    ) -> list[MessageRecord]:
        clauses = ["chat_id = ?"]
        params: list[object] = [chat_id]
        if start_at is not None:
            clauses.append("sent_at >= ?")
            params.append(start_at)
        if end_at is not None:
            clauses.append("sent_at <= ?")
            params.append(end_at)
        if sender_id is not None:
            clauses.append("sender_id = ?")
            params.append(sender_id)
        if sender_name is not None:
            clauses.append("sender_name = ?")
            params.append(sender_name)
        if message_types:
            placeholders = ", ".join("?" for _ in message_types)
            clauses.append(f"message_type IN ({placeholders})")
            params.extend(message_types)
        params.append(max(1, min(int(limit), 5000)))
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM messages
            WHERE {' AND '.join(clauses)}
            ORDER BY sent_at ASC, id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [MessageRecord.from_row(row) for row in rows]

    def latest_successful_reconcile_end(self) -> Optional[str]:
        row = self.connection.execute(
            """
            SELECT window_end
            FROM reconcile_runs
            WHERE status = 'succeeded'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return str(row["window_end"]) if row is not None else None

    def start_reconcile_run(self, start_at: str, end_at: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO reconcile_runs (
                window_start, window_end, started_at, status
            ) VALUES (?, ?, ?, 'running')
            """,
            (start_at, end_at, now_iso(self._timezone)),
        )
        return int(cursor.lastrowid)

    def fail_reconcile_run(self, run_id: int, error: str) -> None:
        self.connection.execute(
            """
            UPDATE reconcile_runs
            SET finished_at = ?, status = 'failed', error = ?
            WHERE id = ?
            """,
            (now_iso(self._timezone), error, run_id),
        )

    def reconcile_batch(
        self,
        run_id: int,
        database_messages: Sequence[ParsedMessage],
        *,
        start_at: str,
        end_at: str,
        tolerance_seconds: float,
    ) -> ReconcileResult:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            for message in database_messages:
                self._ingest_with_connection(connection, message)

            online = self._messages_for_source(
                connection, "wxauto_online", start_at, end_at
            )
            database = self._messages_for_source(
                connection, "wechat_database", start_at, end_at
            )
            pairs = _match_messages(
                online,
                database,
                tolerance_seconds=tolerance_seconds,
                timezone=self._timezone,
            )
            matched_online = {online_id for online_id, _, _, _ in pairs}
            matched_database = {database_id for _, database_id, _, _ in pairs}
            current = now_iso(self._timezone)

            for online_id, database_id, delta_ms, score in pairs:
                online_row = connection.execute(
                    "SELECT * FROM messages WHERE id = ?", (online_id,)
                ).fetchone()
                database_row = connection.execute(
                    "SELECT * FROM messages WHERE id = ?", (database_id,)
                ).fetchone()
                if online_row is None or database_row is None:
                    raise RuntimeError("对账候选在事务内消失")
                keep_online_file = (
                    online_row["file_status"] == "available"
                    and online_row["file_path"] is not None
                )
                file_path = (
                    online_row["file_path"]
                    if keep_online_file
                    else database_row["file_path"]
                )
                file_status = (
                    online_row["file_status"]
                    if keep_online_file
                    else database_row["file_status"]
                )
                connection.execute(
                    """
                    UPDATE messages
                    SET sent_at = ?,
                        sent_at_source = 'wechat_database',
                        chat_id = ?,
                        chat_type = ?,
                        sender_id = ?,
                        sender_name = ?,
                        sender_remark = COALESCE(?, sender_remark),
                        direction = ?,
                        message_type = ?,
                        content = ?,
                        file_path = ?,
                        file_status = ?,
                        mentioned_agent = ?,
                        ingest_source = 'reconciled',
                        reconcile_status = 'matched',
                        raw_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        database_row["sent_at"],
                        database_row["chat_id"],
                        database_row["chat_type"],
                        database_row["sender_id"],
                        database_row["sender_name"],
                        database_row["sender_remark"],
                        database_row["direction"],
                        database_row["message_type"],
                        database_row["content"],
                        file_path,
                        file_status,
                        database_row["mentioned_agent"],
                        database_row["raw_json"],
                        current,
                        online_id,
                    ),
                )
                connection.execute(
                    "UPDATE message_sources SET message_id = ? WHERE message_id = ?",
                    (online_id, database_id),
                )
                connection.execute(
                    """
                    INSERT INTO message_reconciliations (
                        canonical_message_id, online_message_id,
                        database_message_id, match_score, time_delta_ms,
                        match_method, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'time_content_identity', ?)
                    """,
                    (
                        online_id,
                        online_id,
                        database_id,
                        score,
                        delta_ms,
                        current,
                    ),
                )
                connection.execute("DELETE FROM messages WHERE id = ?", (database_id,))

            online_ids = [record.id for record in online if record.id not in matched_online]
            database_ids = [
                record.id for record in database if record.id not in matched_database
            ]
            self._mark_status(connection, online_ids, "online_only", current)
            self._mark_status(connection, database_ids, "database_only", current)

            result = ReconcileResult(
                status="succeeded",
                online_count=len(online),
                database_count=len(database),
                matched_count=len(pairs),
                online_only_count=len(online_ids),
                database_only_count=len(database_ids),
            )
            connection.execute(
                """
                UPDATE reconcile_runs
                SET finished_at = ?, status = 'succeeded',
                    online_count = ?, database_count = ?, matched_count = ?,
                    online_only_count = ?, database_only_count = ?
                WHERE id = ?
                """,
                (
                    current,
                    result.online_count,
                    result.database_count,
                    result.matched_count,
                    result.online_only_count,
                    result.database_only_count,
                    run_id,
                ),
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

    def _messages_for_source(
        self,
        connection: sqlite3.Connection,
        source_type: str,
        start_at: str,
        end_at: str,
    ) -> list[MessageRecord]:
        rows = connection.execute(
            """
            SELECT DISTINCT m.*
            FROM messages AS m
            JOIN message_sources AS s ON s.message_id = m.id
            WHERE s.source_type = ?
              AND m.sent_at >= ?
              AND m.sent_at <= ?
              AND m.reconcile_status != 'matched'
            ORDER BY m.sent_at ASC, m.id ASC
            """,
            (source_type, start_at, end_at),
        ).fetchall()
        return [MessageRecord.from_row(row) for row in rows]

    @staticmethod
    def _mark_status(
        connection: sqlite3.Connection,
        message_ids: Sequence[int],
        status: str,
        updated_at: str,
    ) -> None:
        if not message_ids:
            return
        placeholders = ", ".join("?" for _ in message_ids)
        connection.execute(
            f"""
            UPDATE messages
            SET reconcile_status = ?, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            [status, updated_at, *message_ids],
        )


def _normalize_content(record: MessageRecord) -> str:
    value = (record.content or "").replace("\r\n", "\n").strip()
    if record.message_type != "text":
        return f"[{record.message_type}]"
    value = re.sub(r"@\s+", "@", value)
    return " ".join(value.split())


def _compatible(left: MessageRecord, right: MessageRecord) -> tuple[bool, bool]:
    if left.chat_id != right.chat_id:
        return False, False
    sender_id_match = bool(
        left.sender_id and right.sender_id and left.sender_id == right.sender_id
    )
    if left.sender_id and right.sender_id:
        if not sender_id_match:
            return False, False
    elif left.sender_name != right.sender_name:
        return False, False
    if left.message_type != right.message_type:
        return False, sender_id_match
    return _normalize_content(left) == _normalize_content(right), sender_id_match


def _match_messages(
    online: Sequence[MessageRecord],
    database: Sequence[MessageRecord],
    *,
    tolerance_seconds: float,
    timezone: tzinfo,
) -> list[tuple[int, int, int, float]]:
    unmatched_database = {record.id: record for record in database}
    result: list[tuple[int, int, int, float]] = []
    tolerance_ms = int(tolerance_seconds * 1000)

    for online_record in online:
        online_time = normalize_datetime(online_record.sent_at, timezone)
        candidates: list[tuple[int, int, int, MessageRecord]] = []
        for database_record in unmatched_database.values():
            compatible, sender_id_match = _compatible(online_record, database_record)
            if not compatible:
                continue
            database_time = normalize_datetime(database_record.sent_at, timezone)
            delta_ms = int(abs((database_time - online_time).total_seconds()) * 1000)
            if delta_ms > tolerance_ms:
                continue
            exact_content = int(
                (online_record.content or "").strip()
                == (database_record.content or "").strip()
            )
            candidates.append(
                (delta_ms, -int(sender_id_match), -exact_content, database_record)
            )
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3].id))
        best = candidates[0]
        if len(candidates) > 1 and candidates[1][:3] == best[:3]:
            continue
        database_record = best[3]
        score = max(0.0, 1.0 - (best[0] / max(tolerance_ms, 1)))
        result.append((online_record.id, database_record.id, best[0], score))
        unmatched_database.pop(database_record.id, None)
    return result
