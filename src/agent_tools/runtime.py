"""Runtime binding between Hermes tools and the connected wx-auto adapter."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Optional, Protocol

from data_manager.models import MessageRecord


class QueryWorker(Protocol):
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
    ) -> list[MessageRecord]: ...


@dataclass(frozen=True)
class ToolRuntime:
    worker: QueryWorker
    timezone: tzinfo


@dataclass(frozen=True)
class BindingToken:
    generation: int


_lock = threading.Lock()
_generation = 0
_active_token: Optional[BindingToken] = None
_active_runtime: Optional[ToolRuntime] = None


def bind_data_worker(worker: QueryWorker, timezone: tzinfo) -> BindingToken:
    """Bind the most recently connected gateway adapter to the agent tools."""
    global _generation, _active_token, _active_runtime
    with _lock:
        _generation += 1
        token = BindingToken(_generation)
        _active_token = token
        _active_runtime = ToolRuntime(worker=worker, timezone=timezone)
        return token


def unbind_data_worker(token: BindingToken) -> bool:
    """Unbind only when *token* still owns the active runtime."""
    global _active_token, _active_runtime
    with _lock:
        if token != _active_token:
            return False
        _active_token = None
        _active_runtime = None
        return True


def get_tool_runtime() -> Optional[ToolRuntime]:
    """Return an immutable snapshot of the current tool runtime."""
    with _lock:
        return _active_runtime
