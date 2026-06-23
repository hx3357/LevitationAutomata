"""DataManager 同步、查询和对账编排。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from data_manager.models import (
    IngestResult,
    MessageRecord,
    ParsedMessage,
    ReconcileResult,
    SyncResult,
)
from data_manager.normalizer import normalize_online_message
from data_manager.parsers import NullWeChatDatabaseParser, WeChatDatabaseParser
from data_manager.repository import SQLiteMessageRepository
from data_manager.time_utils import normalize_datetime, normalize_iso, resolve_timezone

if TYPE_CHECKING:
    from config import WxAutoConfig

logger = logging.getLogger(__name__)


class DataManager:
    def __init__(
        self,
        cfg: "WxAutoConfig",
        *,
        plugin_root: Path,
        parser: Optional[WeChatDatabaseParser] = None,
    ) -> None:
        self._cfg = cfg
        self._timezone = resolve_timezone(cfg.data_manager.timezone)
        database_path = Path(cfg.data_manager.database_path)
        if not database_path.is_absolute():
            database_path = plugin_root / database_path
        self._repository = SQLiteMessageRepository(database_path, self._timezone)
        self._parser = parser or NullWeChatDatabaseParser()
        self._started_at = datetime.now(self._timezone)

    def start(self) -> SyncResult:
        self._repository.open()
        try:
            return self.sync_from_database(sync_type="startup")
        except Exception:
            self._repository.close()
            raise

    def stop(self) -> None:
        self._repository.close()

    def ingest_online(self, raw_message: dict) -> IngestResult:
        chat = str(raw_message.get("chat", ""))
        entry = self._cfg.chat_entry(chat)
        if entry is not None:
            chat_type = "group" if entry.type == "group" else "dm"
        elif raw_message.get("type") == "self":
            chat_type = "dm"
        else:
            chat_type = (
                "dm" if chat == str(raw_message.get("sender", "")) else "group"
            )
        parsed = normalize_online_message(
            raw_message,
            chat_type=chat_type,
            timezone=self._timezone,
            agent_names=self._cfg.data_manager.agent_names,
        )
        return self._repository.ingest(parsed)

    def sync_from_database(
        self,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        *,
        sync_type: str = "scheduled",
    ) -> SyncResult:
        run_id = self._repository.start_sync_run(sync_type)
        count = 0
        try:
            validation = self._parser.validate_source()
            if not validation.valid:
                list(self._parser.iter_messages(start_at, end_at))
                error = validation.reason or "微信数据库解析源不可用"
                self._repository.finish_sync_run(
                    run_id,
                    status="no_parser",
                    message_count=0,
                    error=error,
                )
                return SyncResult("no_parser", 0, error)
            for message in self._parser.iter_messages(start_at, end_at):
                message = self._normalize_database_message(message)
                self._validate_parsed_message(message)
                result = self._repository.ingest(message)
                count += int(result.inserted)
        except Exception as exc:
            logger.exception("[wx-data] 微信数据库同步失败")
            self._repository.finish_sync_run(
                run_id,
                status="failed",
                message_count=count,
                error=str(exc),
            )
            return SyncResult("failed", count, str(exc))
        self._repository.finish_sync_run(
            run_id,
            status="succeeded",
            message_count=count,
        )
        return SyncResult("succeeded", count)

    def reconcile(
        self,
        start_at: datetime | str,
        end_at: datetime | str,
    ) -> ReconcileResult:
        start_iso = normalize_iso(start_at, self._timezone)
        end_iso = normalize_iso(end_at, self._timezone)
        run_id = self._repository.start_reconcile_run(start_iso, end_iso)
        try:
            validation = self._parser.validate_source()
            if not validation.valid:
                error = validation.reason or "微信数据库解析源不可用"
                self._repository.fail_reconcile_run(run_id, error)
                logger.warning("[wx-data] 跳过对账：%s", error)
                return ReconcileResult("failed", error=error)
            messages = [
                self._normalize_database_message(message)
                for message in self._parser.iter_messages(
                    normalize_datetime(start_at, self._timezone),
                    normalize_datetime(end_at, self._timezone),
                )
            ]
            self._validate_reconcile_batch(messages, start_iso, end_iso)
            return self._repository.reconcile_batch(
                run_id,
                messages,
                start_at=start_iso,
                end_at=end_iso,
                tolerance_seconds=(
                    self._cfg.data_manager.reconciliation.time_tolerance_seconds
                ),
            )
        except Exception as exc:
            logger.exception(
                "[wx-data] 对账批次失败，已有消息保持不变 start=%s end=%s",
                start_iso,
                end_iso,
            )
            try:
                self._repository.fail_reconcile_run(run_id, str(exc))
            except Exception:
                logger.exception("[wx-data] 无法记录失败的对账批次")
            return ReconcileResult("failed", error=str(exc))

    def reconcile_next_window(self) -> ReconcileResult:
        reconciliation = self._cfg.data_manager.reconciliation
        end_at = datetime.now(self._timezone) - timedelta(
            seconds=reconciliation.settle_delay_seconds
        )
        latest = self._repository.latest_successful_reconcile_end()
        if latest is None:
            start_at = self._started_at
        else:
            start_at = normalize_datetime(latest, self._timezone) - timedelta(
                seconds=reconciliation.overlap_seconds
            )
        if end_at <= start_at:
            return ReconcileResult("succeeded")
        return self.reconcile(start_at, end_at)

    def query_messages(
        self,
        chat_id: str,
        *,
        start_at: datetime | str | None = None,
        end_at: datetime | str | None = None,
        sender_id: Optional[str] = None,
        sender_name: Optional[str] = None,
        message_types: Optional[Sequence[str]] = None,
        limit: int = 200,
    ) -> list[MessageRecord]:
        return self._repository.query_messages(
            chat_id,
            start_at=(
                normalize_iso(start_at, self._timezone)
                if start_at is not None
                else None
            ),
            end_at=(
                normalize_iso(end_at, self._timezone) if end_at is not None else None
            ),
            sender_id=sender_id,
            sender_name=sender_name,
            message_types=message_types,
            limit=limit,
        )

    @staticmethod
    def _validate_parsed_message(message: ParsedMessage) -> None:
        if message.source_type != "wechat_database":
            raise ValueError("数据库解析器只能产出 wechat_database 来源")
        if not message.chat_id or not message.sender_name:
            raise ValueError("数据库消息缺少 chat_id 或 sender_name")

    def _normalize_database_message(self, message: ParsedMessage) -> ParsedMessage:
        return replace(
            message,
            source_type="wechat_database",
            source_message_id=message.source_message_id or None,
            sent_at=normalize_iso(message.sent_at, self._timezone),
            sent_at_source="wechat_database",
            observed_at=normalize_iso(message.observed_at, self._timezone),
        )

    def _validate_reconcile_batch(
        self,
        messages: Sequence[ParsedMessage],
        start_at: str,
        end_at: str,
    ) -> None:
        if len(messages) > 100_000:
            raise ValueError("单次对账消息数量超过 100000")
        if not messages:
            return
        start = normalize_datetime(start_at, self._timezone)
        end = normalize_datetime(end_at, self._timezone)
        tolerance = timedelta(
            seconds=self._cfg.data_manager.reconciliation.time_tolerance_seconds
        )
        replacement_count = 0
        source_ids: list[str] = []
        meaningful_count = 0
        for message in messages:
            self._validate_parsed_message(message)
            sent_at = normalize_datetime(message.sent_at, self._timezone)
            if sent_at < start - tolerance or sent_at > end + tolerance:
                raise ValueError(f"数据库消息时间越界: {message.sent_at}")
            replacement_count += (message.content or "").count("\ufffd")
            meaningful_count += int(
                bool((message.content or "").strip())
                or message.message_type not in {"text", "unknown"}
            )
            if message.source_message_id:
                source_ids.append(message.source_message_id)
        if meaningful_count == 0:
            raise ValueError("数据库消息整批内容为空")
        if replacement_count > max(5, len(messages) // 10):
            raise ValueError("数据库消息包含异常比例的乱码替换字符")
        if source_ids:
            duplicate_ratio = 1.0 - (len(set(source_ids)) / len(source_ids))
            if duplicate_ratio > 0.2:
                raise ValueError("数据库消息来源 ID 重复比例异常")
