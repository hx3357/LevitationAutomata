from __future__ import annotations

import ast
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tenacity import asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_tools import chat_history
from agent_tools.chat_history import (
    register_tools,
    search_recent_wechat_chat_history,
    search_wechat_chat_history,
)
from agent_tools.runtime import (
    bind_data_worker,
    get_tool_runtime,
    unbind_data_worker,
)


TZ = timezone(timedelta(hours=8))
CHAT_NAME = "网友塔顺菲"


class FakeDataWorker:
    def __init__(self, records=None, error: Exception | None = None) -> None:
        self.records = list(records or [])
        self.error = error
        self.calls: list[dict] = []

    async def query_messages(self, chat_id: str, **kwargs):
        self.calls.append({"chat_id": chat_id, **kwargs})
        if self.error is not None:
            raise self.error
        return list(self.records)


class FakePluginContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


def make_record(
    index: int,
    *,
    sender: str = CHAT_NAME,
    content: str | None = "hello",
):
    return SimpleNamespace(
        id=index,
        sent_at=f"2026-06-22T10:{index % 60:02d}:00+08:00",
        sender_name=sender,
        content=content,
    )


def session_env(name: str, default: str = "") -> str:
    values = {
        "HERMES_SESSION_PLATFORM": "wx-auto",
        "HERMES_SESSION_CHAT_ID": CHAT_NAME,
    }
    return values.get(name, default)


class AgentToolRegistrationTests(unittest.TestCase):
    def test_registers_two_tools_without_chat_parameters(self) -> None:
        ctx = FakePluginContext()
        register_tools(ctx)

        self.assertEqual(
            [tool["name"] for tool in ctx.tools],
            [
                "search_wechat_chat_history",
                "search_recent_wechat_chat_history",
            ],
        )
        for tool in ctx.tools:
            self.assertEqual(tool["toolset"], "wx-auto")
            self.assertTrue(tool["is_async"])
            properties = tool["schema"]["parameters"]["properties"]
            self.assertNotIn("chat_id", properties)
            self.assertNotIn("chat_name", properties)


class AgentToolQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.worker = FakeDataWorker()
        self.token = bind_data_worker(self.worker, TZ)
        self.session_patch = patch.object(
            chat_history,
            "_get_session_env",
            side_effect=session_env,
        )
        self.session_patch.start()

    async def asyncTearDown(self) -> None:
        self.session_patch.stop()
        unbind_data_worker(self.token)

    async def test_exact_tool_simulates_registered_call(self) -> None:
        payload = {
            "name": "search_wechat_chat_history",
            "arguments": {
                "start_at": "2026-06-22T00:00:00+08:00",
                "end_at": "2026-06-23T00:00:00+08:00",
                "nickname": CHAT_NAME,
            },
        }

        result = json.loads(
            await search_wechat_chat_history(payload["arguments"])
        )

        print(result)

        self.assertEqual(result, {"messages": [], "truncated": False})
        self.assertEqual(
            self.worker.calls,
            [
                {
                    "chat_id": CHAT_NAME,
                    "start_at": "2026-06-22T00:00:00+08:00",
                    "end_at": "2026-06-23T00:00:00+08:00",
                    "sender_name": CHAT_NAME,
                    "limit": 201,
                }
            ],
        )

    async def test_naive_iso_uses_data_manager_timezone(self) -> None:
        await search_wechat_chat_history(
            {
                "start_at": "2026-06-22T00:00:00",
                "end_at": "2026-06-23T00:00:00",
            }
        )
        call = self.worker.calls[0]
        self.assertEqual(call["start_at"], "2026-06-22T00:00:00+08:00")
        self.assertEqual(call["end_at"], "2026-06-23T00:00:00+08:00")
        self.assertIsNone(call["sender_name"])

    async def test_invalid_iso_and_reversed_range(self) -> None:
        invalid = json.loads(
            await search_wechat_chat_history(
                {"start_at": "not-a-time", "end_at": "2026-06-23T00:00:00"}
            )
        )
        reversed_result = json.loads(
            await search_wechat_chat_history(
                {
                    "start_at": "2026-06-24T00:00:00",
                    "end_at": "2026-06-23T00:00:00",
                }
            )
        )
        self.assertEqual(invalid["error"]["code"], "invalid_arguments")
        self.assertEqual(
            reversed_result["error"]["code"],
            "invalid_time_range",
        )
        self.assertEqual(self.worker.calls, [])

    async def test_recent_tool_simulates_24_hour_call(self) -> None:
        fixed_now = datetime(2026, 6, 23, 12, 0, 0, tzinfo=TZ)
        with patch.object(chat_history, "_now", return_value=fixed_now):
            result = json.loads(
                await search_recent_wechat_chat_history(
                    {"lookback_value": 24, "lookback_unit": "hours"}
                )
            )

        self.assertEqual(result, {"messages": [], "truncated": False})
        self.assertEqual(
            self.worker.calls[0],
            {
                "chat_id": CHAT_NAME,
                "start_at": "2026-06-22T12:00:00+08:00",
                "end_at": "2026-06-23T12:00:00+08:00",
                "sender_name": None,
                "limit": 201,
            },
        )

    async def test_recent_minutes_hours_days_and_nickname(self) -> None:
        fixed_now = datetime(2026, 6, 23, 12, 0, 0, tzinfo=TZ)
        with patch.object(chat_history, "_now", return_value=fixed_now):
            for value, unit, expected in (
                (30, "minutes", "2026-06-23T11:30:00+08:00"),
                (12, "hours", "2026-06-23T00:00:00+08:00"),
                (7, "days", "2026-06-16T12:00:00+08:00"),
            ):
                await search_recent_wechat_chat_history(
                    {
                        "lookback_value": value,
                        "lookback_unit": unit,
                        "nickname": CHAT_NAME,
                    }
                )
                self.assertEqual(self.worker.calls[-1]["start_at"], expected)
                self.assertEqual(
                    self.worker.calls[-1]["sender_name"],
                    CHAT_NAME,
                )

    async def test_recent_rejects_invalid_duration(self) -> None:
        for args in (
            {"lookback_value": 0, "lookback_unit": "hours"},
            {"lookback_value": -1, "lookback_unit": "hours"},
            {"lookback_value": 1.5, "lookback_unit": "hours"},
            {"lookback_value": True, "lookback_unit": "hours"},
            {"lookback_value": 1, "lookback_unit": "weeks"},
        ):
            result = json.loads(
                await search_recent_wechat_chat_history(args)
            )
            self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertEqual(self.worker.calls, [])

    async def test_output_is_compact_and_truncated_at_200(self) -> None:
        self.worker.records = [
            make_record(i, content=None if i == 0 else f"message-{i}")
            for i in range(201)
        ]
        result_text = await search_wechat_chat_history(
            {
                "start_at": "2026-06-22T00:00:00+08:00",
                "end_at": "2026-06-23T00:00:00+08:00",
            }
        )
        result = json.loads(result_text)

        self.assertNotIn(" ", result_text)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["messages"]), 200)
        self.assertEqual(
            result["messages"][0],
            {
                "sent_at": "2026-06-22T10:00:00+08:00",
                "sender": CHAT_NAME,
                "content": "",
            },
        )
        self.assertEqual(
            set(result["messages"][0]),
            {"sent_at", "sender", "content"},
        )

    async def test_exactly_200_is_not_truncated(self) -> None:
        self.worker.records = [make_record(i) for i in range(200)]
        result = json.loads(
            await search_wechat_chat_history(
                {
                    "start_at": "2026-06-22T00:00:00+08:00",
                    "end_at": "2026-06-23T00:00:00+08:00",
                }
            )
        )
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["messages"]), 200)

    async def test_context_and_query_errors_are_structured(self) -> None:
        with patch.object(
            chat_history,
            "_get_session_env",
            side_effect=lambda name, default="": (
                "telegram"
                if name == "HERMES_SESSION_PLATFORM"
                else CHAT_NAME
            ),
        ):
            invalid_platform = json.loads(
                await search_wechat_chat_history(
                    {
                        "start_at": "2026-06-22T00:00:00",
                        "end_at": "2026-06-23T00:00:00",
                    }
                )
            )
        self.assertEqual(
            invalid_platform["error"]["code"],
            "invalid_platform",
        )

        with patch.object(
            chat_history,
            "_get_session_env",
            side_effect=lambda name, default="": (
                "wx-auto" if name == "HERMES_SESSION_PLATFORM" else ""
            ),
        ):
            missing_chat = json.loads(
                await search_wechat_chat_history(
                    {
                        "start_at": "2026-06-22T00:00:00",
                        "end_at": "2026-06-23T00:00:00",
                    }
                )
            )
        self.assertEqual(
            missing_chat["error"]["code"],
            "missing_chat_context",
        )

        self.worker.error = RuntimeError("database path must stay private")
        query_failed = json.loads(
            await search_wechat_chat_history(
                {
                    "start_at": "2026-06-22T00:00:00",
                    "end_at": "2026-06-23T00:00:00",
                }
            )
        )
        self.assertEqual(query_failed["error"]["code"], "query_failed")
        self.assertNotIn("database path", json.dumps(query_failed))

    async def test_unbound_data_manager_returns_structured_error(self) -> None:
        unbind_data_worker(self.token)
        result = json.loads(
            await search_wechat_chat_history(
                {
                    "start_at": "2026-06-22T00:00:00",
                    "end_at": "2026-06-23T00:00:00",
                }
            )
        )
        self.assertEqual(
            result["error"]["code"],
            "data_manager_unavailable",
        )


class ToolRuntimeLifecycleTests(unittest.TestCase):
    def test_old_adapter_cannot_unbind_new_adapter(self) -> None:
        first = FakeDataWorker()
        second = FakeDataWorker()
        first_token = bind_data_worker(first, TZ)
        second_token = bind_data_worker(second, TZ)
        try:
            self.assertFalse(unbind_data_worker(first_token))
            runtime = get_tool_runtime()
            self.assertIsNotNone(runtime)
            self.assertIsNotNone(runtime)
            if runtime is None:
                self.fail("expected the second adapter runtime to remain bound")
            self.assertIs(runtime.worker, second)
            self.assertTrue(unbind_data_worker(second_token))
            self.assertIsNone(get_tool_runtime())
        finally:
            unbind_data_worker(first_token)
            unbind_data_worker(second_token)


class StandaloneAdapterTests(unittest.TestCase):
    def test_standalone_sender_disables_agent_tool_binding(self) -> None:
        adapter_path = Path(__file__).resolve().parents[1] / "adapter.py"
        tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
        standalone = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_standalone_send"
        )
        constructor = next(
            node
            for node in ast.walk(standalone)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "WxautoPlatformAdapter"
        )
        keywords = {keyword.arg: keyword.value for keyword in constructor.keywords}
        self.assertIn("bind_agent_tools", keywords)
        self.assertIsInstance(keywords["bind_agent_tools"], ast.Constant)
        binding_value = cast(ast.Constant, keywords["bind_agent_tools"])
        self.assertIs(binding_value.value, False)


if __name__ == "__main__":
    #unittest.main()
    obj = AgentToolQueryTests()
    asyncio.run(obj.test_exact_tool_simulates_registered_call)
