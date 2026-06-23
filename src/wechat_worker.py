"""单线程 COM worker — 在同一个 CoInitialize 线程内串行执行所有 wxauto 调用。

微信自动化依赖 COM/UIAutomation，必须在同一个已 CoInitialize 的线程上执行。
本模块将一个专用后台线程与 asyncio 事件循环桥接：

    worker = WeChatWorker(cfg)
    await worker.start()
    msgs = await worker.submit(worker.wx.GetListenMessage)
    await worker.stop()

设计约束（与 throttled_wechat.py 保持一致）：
    - wxauto 层全同步阻塞；asyncio 桥接在此层完成。
    - 不在 wxauto 子类里引入 async。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from throttled_wechat import ThrottledWeChat

logger = logging.getLogger(__name__)


@dataclass
class _WorkItem:
    fn: Callable
    args: tuple
    kwargs: dict
    future: asyncio.Future


@dataclass
class WorkerStats:
    uptime_seconds: float
    listen_chats: list[str]
    min_delay: float
    max_delay: float
    seen_ids_count: int
    running: bool


class WeChatWorker:
    """在专用线程内持有 ``ThrottledWeChat`` 实例，对外提供可等待的 ``submit``。"""

    def __init__(self, cfg) -> None:
        """cfg 为 WxAutoConfig 实例。"""
        self._cfg = cfg
        self._work_queue: queue.Queue[Optional[_WorkItem]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wx: Optional[ThrottledWeChat] = None            # ThrottledWeChat 实例
        self._listen_chats: list[str] = []
        self._start_time: float = 0.0
        self._running = False
        self._restart_lock = asyncio.Lock()
        # 用于外部查询的 seen_ids 数量（由轮询方设置）
        self.seen_ids_count: int = 0

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 COM worker 线程，构造 ThrottledWeChat。"""
        self._loop = asyncio.get_running_loop()
        ready = threading.Event()
        error_box: list[Exception] = []

        def _run():
            try:
                # 尝试初始化 COM（Windows 专属）
                try:
                    import pythoncom  # type: ignore
                    pythoncom.CoInitialize()
                except ImportError:
                    pass  # 非 Windows 环境下跳过（仅用于单元测试）

                from throttled_wechat import ThrottledWeChat  # noqa: PLC0415
                self._wx = ThrottledWeChat(
                    min_delay=self._cfg.min_delay,
                    max_delay=self._cfg.max_delay,
                )
                self._start_time = time.monotonic()
                self._running = True
                ready.set()
                self._worker_loop()
            except Exception as exc:
                error_box.append(exc)
                ready.set()
            finally:
                try:
                    import pythoncom  # type: ignore
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, name="wx-com-worker", daemon=True)
        self._thread.start()
        await asyncio.get_running_loop().run_in_executor(None, ready.wait)
        if error_box:
            raise RuntimeError(f"WeChatWorker 启动失败: {error_box[0]}") from error_box[0]

    async def stop(self) -> None:
        """停止 worker 线程。"""
        self._running = False
        self._work_queue.put(None)  # 毒丸
        if self._thread:
            await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 5.0)
            self._thread = None

    async def restart(self) -> None:
        """重建 ThrottledWeChat 实例（/restartwx 使用）。"""
        if not self._wx:
            raise RuntimeError("WeChatWorker 未启动，无法重启")
        async with self._restart_lock:
            await self.stop()
            self._work_queue = queue.Queue()
            await self.start()
            # 重新注册所有监听聊天
            for chat in self._listen_chats:
                await self.submit(self._wx.AddListenChat, chat, savepic=True)
            logger.info("[wx-auto] WeChatWorker 重启完成，已恢复监听 %s", self._listen_chats)

    # ── 监听聊天管理 ──────────────────────────────────────────────────────────

    async def add_listen_chat(self, who: str, savepic: bool = True) -> None:
        if not self._wx:
            raise RuntimeError("WeChatWorker 未启动，无法添加监听聊天")
        await self.submit(self._wx.AddListenChat, who, savepic=savepic)
        if who not in self._listen_chats:
            self._listen_chats.append(who)

    # ── 任务提交 ──────────────────────────────────────────────────────────────

    async def submit(self, fn: Callable, *args, **kwargs) -> Any:
        """将 fn(*args, **kwargs) 提交到 COM 线程执行，返回结果。"""
        if not self._running:
            raise RuntimeError("WeChatWorker 未启动")
        loop = self._loop or asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._work_queue.put(_WorkItem(fn=fn, args=args, kwargs=kwargs, future=fut))
        return await fut

    # ── 统计 ──────────────────────────────────────────────────────────────────

    def stats(self) -> WorkerStats:
        return WorkerStats(
            uptime_seconds=time.monotonic() - self._start_time if self._start_time else 0.0,
            listen_chats=list(self._listen_chats),
            min_delay=self._cfg.min_delay,
            max_delay=self._cfg.max_delay,
            seen_ids_count=self.seen_ids_count,
            running=self._running,
        )

    # ── 内部 ──────────────────────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        """COM 线程主循环：依次执行队列中的任务。"""
        loop = self._loop
        assert loop is not None
        while True:
            item = self._work_queue.get()
            logger.debug("[wx-auto] Worker loop got item: %s", item)
            if item is None:
                break
            try:
                result = item.fn(*item.args, **item.kwargs)
                loop.call_soon_threadsafe(item.future.set_result, result)
            except Exception as exc:
                loop.call_soon_threadsafe(item.future.set_exception, exc)
