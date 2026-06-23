"""Hermes agent tools for querying the current WeChat chat history."""

from __future__ import annotations

import json
import logging
from importlib import import_module
from datetime import datetime, timedelta, tzinfo
from typing import Any, Callable, Optional

from data_manager.models import MessageRecord
from data_manager.time_utils import normalize_datetime, normalize_iso

from agent_tools.runtime import get_tool_runtime

logger = logging.getLogger(__name__)

PLATFORM_NAME = "wx-auto"
TOOLSET_NAME = "wx-auto"
RESULT_LIMIT = 200
QUERY_LIMIT = RESULT_LIMIT + 1

SEARCH_WECHAT_CHAT_HISTORY_SCHEMA: dict[str, Any] = {
    "name": "search_wechat_chat_history",
    "description": (
        "Search messages in the current WeChat conversation within an exact "
        "ISO 8601 time range. The conversation is determined automatically "
        "from the current Hermes session; do not ask for or provide a chat ID."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "start_at": {
                "type": "string",
                "description": (
                    "Inclusive ISO 8601 start time. A missing timezone uses "
                    "the wx-auto DataManager timezone."
                ),
            },
            "end_at": {
                "type": "string",
                "description": (
                    "Inclusive ISO 8601 end time. A missing timezone uses "
                    "the wx-auto DataManager timezone."
                ),
            },
            "nickname": {
                "type": "string",
                "description": (
                    "Optional exact sender display name. Omit to include all "
                    "senders in the current conversation."
                ),
            },
        },
        "required": ["start_at", "end_at"],
        "additionalProperties": False,
    },
}

SEARCH_RECENT_WECHAT_CHAT_HISTORY_SCHEMA: dict[str, Any] = {
    "name": "search_recent_wechat_chat_history",
    "description": (
        "Search recent messages in the current WeChat conversation using a "
        "lookback duration from the tool execution time. The conversation is "
        "determined automatically from the current Hermes session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lookback_value": {
                "type": "integer",
                "minimum": 1,
                "description": "Positive whole-number lookback duration.",
            },
            "lookback_unit": {
                "type": "string",
                "enum": ["minutes", "hours", "days"],
                "description": "Unit for lookback_value.",
            },
            "nickname": {
                "type": "string",
                "description": (
                    "Optional exact sender display name. Omit to include all "
                    "senders in the current conversation."
                ),
            },
        },
        "required": ["lookback_value", "lookback_unit"],
        "additionalProperties": False,
    },
}


def _get_session_env(name: str, default: str = "") -> str:
    session_context = import_module("gateway.session_context")
    return str(session_context.get_session_env(name, default))


def _json_result(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _error(code: str, message: str) -> str:
    return _json_result({"error": {"code": code, "message": message}})


def _resolve_current_chat() -> tuple[Optional[str], Optional[str]]:
    platform = _get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if platform != PLATFORM_NAME:
        return None, _error(
            "invalid_platform",
            "This tool is only available from a wx-auto WeChat session.",
        )
    chat_id = _get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    if not chat_id:
        return None, _error(
            "missing_chat_context",
            "The current Hermes session does not include a WeChat chat ID.",
        )
    return chat_id, None


def _parse_iso(value: Any, field_name: str, timezone: tzinfo) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty ISO 8601 string")
    try:
        return normalize_datetime(value.strip(), timezone)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 time") from exc


def _normalize_nickname(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("nickname must be a string")
    normalized = value.strip()
    return normalized or None


def _serialize_messages(records: list[MessageRecord]) -> str:
    truncated = len(records) > RESULT_LIMIT
    messages = [
        {
            "sent_at": record.sent_at,
            "sender": record.sender_name,
            "content": record.content or "",
        }
        for record in records[:RESULT_LIMIT]
    ]
    return _json_result({"messages": messages, "truncated": truncated})


async def _query_current_chat(
    *,
    start_at: datetime,
    end_at: datetime,
    nickname: Optional[str],
) -> str:
    chat_id, context_error = _resolve_current_chat()
    if context_error is not None:
        return context_error

    runtime = get_tool_runtime()
    if runtime is None:
        return _error(
            "data_manager_unavailable",
            "The wx-auto DataManager is disabled, not ready, or disconnected.",
        )

    try:
        records = await runtime.worker.query_messages(
            chat_id or "",
            start_at=normalize_iso(start_at, runtime.timezone),
            end_at=normalize_iso(end_at, runtime.timezone),
            sender_name=nickname,
            limit=QUERY_LIMIT,
        )
    except Exception:
        logger.exception("[wx-auto] Agent chat history query failed")
        return _error(
            "query_failed",
            "The WeChat chat history query could not be completed.",
        )
    return _serialize_messages(records)


async def search_wechat_chat_history(args: dict[str, Any], **_: Any) -> str:
    runtime = get_tool_runtime()
    if runtime is None:
        return _error(
            "data_manager_unavailable",
            "The wx-auto DataManager is disabled, not ready, or disconnected.",
        )
    try:
        start_at = _parse_iso(args.get("start_at"), "start_at", runtime.timezone)
        end_at = _parse_iso(args.get("end_at"), "end_at", runtime.timezone)
        nickname = _normalize_nickname(args.get("nickname"))
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    if start_at > end_at:
        return _error(
            "invalid_time_range",
            "start_at must be earlier than or equal to end_at.",
        )
    return await _query_current_chat(
        start_at=start_at,
        end_at=end_at,
        nickname=nickname,
    )


async def search_recent_wechat_chat_history(
    args: dict[str, Any],
    **_: Any,
) -> str:
    runtime = get_tool_runtime()
    if runtime is None:
        return _error(
            "data_manager_unavailable",
            "The wx-auto DataManager is disabled, not ready, or disconnected.",
        )

    value = args.get("lookback_value")
    unit = args.get("lookback_unit")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return _error(
            "invalid_arguments",
            "lookback_value must be a positive integer.",
        )
    if unit not in {"minutes", "hours", "days"}:
        return _error(
            "invalid_arguments",
            "lookback_unit must be one of: minutes, hours, days.",
        )
    try:
        nickname = _normalize_nickname(args.get("nickname"))
        delta = timedelta(**{unit: value})
    except (OverflowError, ValueError) as exc:
        return _error("invalid_arguments", str(exc))

    end_at = _now(runtime.timezone)
    start_at = end_at - delta
    return await _query_current_chat(
        start_at=start_at,
        end_at=end_at,
        nickname=nickname,
    )


def _now(timezone: tzinfo) -> datetime:
    return datetime.now(timezone)


def register_tools(ctx: Any) -> None:
    tools: tuple[tuple[str, dict[str, Any], Callable[..., Any]], ...] = (
        (
            "search_wechat_chat_history",
            SEARCH_WECHAT_CHAT_HISTORY_SCHEMA,
            search_wechat_chat_history,
        ),
        (
            "search_recent_wechat_chat_history",
            SEARCH_RECENT_WECHAT_CHAT_HISTORY_SCHEMA,
            search_recent_wechat_chat_history,
        ),
    )
    for name, schema, handler in tools:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET_NAME,
            schema=schema,
            handler=handler,
            is_async=True,
            emoji="🔎",
        )
