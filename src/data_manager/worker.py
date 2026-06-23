"""将阻塞 SQLite/解析器操作桥接到单线程 executor。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING, TypeVar

from data_manager.models import (
    IngestResult,
    MessageRecord,
    ReconcileResult,
    SyncResult,
)
from data_manager.parsers import WeChatDatabaseParser
from data_manager.service import DataManager

if TYPE_CHECKING:
    from config import WxAutoConfig

_T = TypeVar("_T")


class DataWorker:
    def __init__(
        self,
        cfg: "WxAutoConfig",
        *,
        plugin_root: Path,
        parser: Optional[WeChatDatabaseParser] = None,
    ) -> None:
        self._manager = DataManager(cfg, plugin_root=plugin_root, parser=parser)
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running = False

    async def start(self) -> SyncResult:
        if self._running:
            raise RuntimeError("DataWorker 已启动")
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="wx-data-worker",
        )
        try:
            result = await self._submit(self._manager.start, allow_starting=True)
        except Exception:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
            raise
        self._running = True
        return result

    async def stop(self) -> None:
        if self._executor is None:
            return
        try:
            if self._running:
                await self._submit(self._manager.stop)
        finally:
            self._running = False
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor = None

    async def ingest_online(self, raw_message: dict) -> IngestResult:
        return await self._submit(self._manager.ingest_online, raw_message)

    async def sync_from_database(
        self,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        *,
        sync_type: str = "scheduled",
    ) -> SyncResult:
        return await self._submit(
            self._manager.sync_from_database,
            start_at,
            end_at,
            sync_type=sync_type,
        )

    async def reconcile(
        self,
        start_at: datetime | str,
        end_at: datetime | str,
    ) -> ReconcileResult:
        return await self._submit(self._manager.reconcile, start_at, end_at)

    async def reconcile_next_window(self) -> ReconcileResult:
        return await self._submit(self._manager.reconcile_next_window)

    async def query_messages(
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
        return await self._submit(
            self._manager.query_messages,
            chat_id,
            start_at=start_at,
            end_at=end_at,
            sender_id=sender_id,
            sender_name=sender_name,
            message_types=message_types,
            limit=limit,
        )

    async def _submit(
        self,
        fn: Callable[..., _T],
        *args: Any,
        allow_starting: bool = False,
        **kwargs: Any,
    ) -> _T:
        executor = self._executor
        if executor is None or (not self._running and not allow_starting):
            raise RuntimeError("DataWorker 未启动")
        loop = asyncio.get_running_loop()
        if kwargs:
            call = lambda: fn(*args, **kwargs)
            return await loop.run_in_executor(executor, call)
        return await loop.run_in_executor(executor, fn, *args)
