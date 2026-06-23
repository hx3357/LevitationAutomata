"""WX Auto Hermes Platform Adapter。

数据流：
    用户 <-> 微信 <-> WxautoPlatformAdapter <-> Gateway Runner <-> AIAgent

入站消息经过滤器管道（filters.py）处理后，组装为 MessageEvent 交给网关。
出站消息通过 send() 经单线程 COM worker（wechat_worker.py）调用 ThrottledWeChat.SendMsg。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# src/ 放在本文件旁边；将其加入路径，使 src 内模块可作为扁平包导入
_SRC = str(Path(__file__).parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform, PlatformConfig

from agent_tools import (
    BindingToken,
    bind_data_worker,
    register_tools,
    unbind_data_worker,
)
from background_process import BackgroundProcessManager
from background_registry import register_all
from config import WxAutoConfig, load_config
from commands import _registry as command_registry
from data_manager import DataWorker, PyWxDumpParser
from data_manager.time_utils import resolve_timezone
from directives import Directive, Text, has_directive_syntax, parse
from filters import FilterOutput, FilterResult, apply_filters
from markdown_utils import strip_markdown
from wechat_worker import WeChatWorker

logger = logging.getLogger(__name__)

PLATFORM_NAME = "wx-auto"
# 已处理消息 ID 的滑动窗口大小（防重复投递）
_SEEN_ID_LIMIT = 2000


# ─────────────────────────────────────────────────────────────────────────────
# Adapter
# ─────────────────────────────────────────────────────────────────────────────

class WxautoPlatformAdapter(BasePlatformAdapter):
    """将 wxauto（同步 COM）桥接至 Hermes 异步网关的适配器。"""

    def __init__(
        self,
        config: PlatformConfig,
        *,
        bind_agent_tools: bool = True,
    ):
        super().__init__(config, Platform(PLATFORM_NAME))

        extra = config.extra or {}
        config_path: Optional[str] = os.getenv("WX_AUTO_CONFIG_PATH") or extra.get("config_path")
        self._cfg: WxAutoConfig = load_config(config_path)
        self._worker: WeChatWorker = WeChatWorker(self._cfg)
        self._data_worker: Optional[DataWorker] = None
        if self._cfg.data_manager.enabled:
            parser = None
            pywxdump_cfg = self._cfg.data_manager.pywxdump
            database_key = os.getenv("WX_AUTO_WECHAT_DB_KEY", "").strip()

            if pywxdump_cfg.enabled:
                if pywxdump_cfg.wx_path and database_key:
                    plugin_root = Path(__file__).parent

                    def resolve_plugin_path(value: str) -> Path:
                        path = Path(value)
                        return path if path.is_absolute() else plugin_root / path

                    parser = PyWxDumpParser(
                        wx_path=resolve_plugin_path(pywxdump_cfg.wx_path),
                        merge_path=resolve_plugin_path(pywxdump_cfg.merge_path),
                        media_cache_path=resolve_plugin_path(
                            pywxdump_cfg.media_cache_path
                        ),
                        chats={
                            entry.id: entry.type for entry in self._cfg.chat_table
                        },
                        timezone=resolve_timezone(
                            self._cfg.data_manager.timezone
                        ),
                        agent_names=self._cfg.data_manager.agent_names,
                        page_size=pywxdump_cfg.page_size,
                        key=database_key,
                    )
                else:
                    logger.warning(
                        "[%s] PyWxDump 已启用但 wx_path 或 "
                        "WX_AUTO_WECHAT_DB_KEY 缺失，使用空数据库解析器",
                        PLATFORM_NAME,
                    )
            self._data_worker = DataWorker(
                self._cfg,
                plugin_root=Path(__file__).parent,
                parser=parser,
            )
        
        self._data_ready = False
        self._bind_agent_tools = bind_agent_tools
        self._tool_binding_token: Optional[BindingToken] = None
        self._proc_manager: BackgroundProcessManager = BackgroundProcessManager()
        register_all(self._proc_manager, self._cfg)
        self._poll_task: Optional[asyncio.Task] = None
        self._reconcile_task: Optional[asyncio.Task] = None
        self._seen_ids: deque[str] = deque(maxlen=_SEEN_ID_LIMIT)
        self._directives: dict[str, Any] = {"file": self._directive_file}

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """启动 COM worker，注册所有监听聊天，开启轮询。"""
        if self._data_worker is not None:
            try:
                sync_result = await self._data_worker.start()
                self._data_ready = True
                logger.info(
                    "[%s] DataManager 已启动，启动同步状态=%s 新增消息=%d",
                    PLATFORM_NAME,
                    sync_result.status,
                    sync_result.message_count,
                )
            except Exception:
                logger.exception(
                    "[%s] DataManager 启动失败，降级为不持久化消息",
                    PLATFORM_NAME,
                )

        try:
            await self._worker.start()
        except Exception:
            logger.exception("[%s] COM worker 启动失败", PLATFORM_NAME)
            if self._data_worker is not None and self._data_ready:
                await self._data_worker.stop()
                self._data_ready = False
            return False

        for entry in self._cfg.chat_table:
            try:
                await self._worker.add_listen_chat(entry.id, savepic=True)
                logger.debug("[%s] 已添加监听: %s", PLATFORM_NAME, entry.id)
            except Exception:
                logger.exception("[%s] AddListenChat 失败: %s", PLATFORM_NAME, entry.id)

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        if (
            self._data_ready
            and self._cfg.data_manager.reconciliation.enabled
        ):
            self._reconcile_task = asyncio.create_task(
                self._reconcile_loop(),
                name="wx-data-reconcile",
            )
        self._mark_connected()
        
        await self._proc_manager.start_all()
        if (
            self._bind_agent_tools
            and self._data_worker is not None
            and self._data_ready
        ):
            self._tool_binding_token = bind_data_worker(
                self._data_worker,
                resolve_timezone(self._cfg.data_manager.timezone),
            )
        logger.info("[%s] 已连接，监听聊天数: %d", PLATFORM_NAME, len(self._cfg.chat_table))
        return True

    async def disconnect(self) -> None:
        """停止轮询、关闭后台进程，并关闭 COM worker。"""
        if self._tool_binding_token is not None:
            unbind_data_worker(self._tool_binding_token)
            self._tool_binding_token = None
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None

        if (
            self._data_worker is not None
            and self._data_ready
            and self._cfg.data_manager.reconciliation.enabled
        ):
            try:
                await self._data_worker.reconcile_next_window()
            except Exception:
                logger.exception("[%s] 关闭前对账失败，继续关闭", PLATFORM_NAME)

        await self._proc_manager.stop_all()
        await self._worker.stop()
        if self._data_worker is not None and self._data_ready:
            await self._data_worker.stop()
            self._data_ready = False
        self._mark_disconnected()
        logger.info("[%s] 已断开", PLATFORM_NAME)

    # ── 入站轮询 ──────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                raw_msgs = await self._fetch_updates()
                for raw in raw_msgs:
                    await self._handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] 轮询循环异常", PLATFORM_NAME)
                await asyncio.sleep(2.0)
            else:
                await asyncio.sleep(self._cfg.poll_interval)

    async def _reconcile_loop(self) -> None:
        """按配置周期执行安全时间窗对账；仅在显式启用时创建。"""
        assert self._data_worker is not None
        interval = self._cfg.data_manager.reconciliation.interval_seconds
        while self._running:
            try:
                result = await self._data_worker.reconcile_next_window()
                if result.status == "failed":
                    logger.warning(
                        "[%s] DataManager 对账失败: %s",
                        PLATFORM_NAME,
                        result.error,
                    )
                else:
                    logger.info(
                        "[%s] DataManager 对账完成 matched=%d online_only=%d "
                        "database_only=%d",
                        PLATFORM_NAME,
                        result.matched_count,
                        result.online_only_count,
                        result.database_only_count,
                    )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] DataManager 周期对账异常", PLATFORM_NAME)
                await asyncio.sleep(interval)

    async def _fetch_updates(self) -> list[dict]:
        """从 COM worker 获取新消息，转换为原始字典列表。"""
        try:
            if self._worker._wx is None:
                raise RuntimeError("COM worker 的 WeChat 实例未就绪")
            listen_msgs = await self._worker.submit(self._worker._wx.GetListenMessage)
        except Exception:
            logger.exception("[%s] GetListenMessage 失败", PLATFORM_NAME)
            return []

        result: list[dict] = []
        now = datetime.now(
            resolve_timezone(self._cfg.data_manager.timezone)
        ).isoformat(timespec="seconds")
        for chat, msg_list in (listen_msgs or {}).items():
            chat_name = chat.who if hasattr(chat, "who") else str(chat)
            for msg in msg_list:
                if msg.type not in ("friend", "self"):
                    continue
                msg_id = str(getattr(msg, "id", ""))
                if msg_id and msg_id in self._seen_ids:
                    continue
                if msg_id:
                    self._seen_ids.append(msg_id)
                sender = msg.sender if isinstance(msg.sender, str) else msg.sender[0]
                raw = {
                    "timestamp": now,
                    "chat": chat_name,
                    "type": msg.type,
                    "sender": sender,
                    "content": msg.content,
                    "id": msg_id,
                }
                sender_remark = getattr(msg, "sender_remark", None)
                if sender_remark is not None:
                    raw["sender_remark"] = sender_remark
                result.append(raw)
        self._worker.seen_ids_count = len(self._seen_ids)
        return result

    async def _handle_raw(self, raw: dict) -> None:
        """对单条原始消息执行过滤 + 组装 + 派发。"""
        if self._data_worker is not None and self._data_ready:
            try:
                await self._data_worker.ingest_online(raw)
            except Exception:
                logger.exception(
                    "[%s] 实时消息落库失败，继续处理消息 chat=%s id=%s",
                    PLATFORM_NAME,
                    raw.get("chat", ""),
                    raw.get("id", ""),
                )

        output: FilterOutput = apply_filters(raw, self._cfg, command_registry)

        if output.result == FilterResult.DROP:
            return

        if output.result == FilterResult.COMMAND_NARRATION:
            # 将命令处理结果回复到原聊天
            if output.command_reply:
                await self._dispatch_command_reply(raw, output)
            # 将旁白注入 agent（只读事件）
            # if output.narration:
            #     await self._notify_agent(output.narration, raw)
            return

        # FilterResult.PASS — 组装 MessageEvent 送入网关
        event = self._build_event(raw)
        await self.handle_message(event)

    async def _dispatch_command_reply(self, raw: dict, output: FilterOutput) -> None:
        """将命令处理结果回复到微信，并处理特殊 __ACTION__ 动作。"""
        reply = output.command_reply or ""
        chat = raw.get("chat", "")

        if reply == "__ACTION__:restartwx":
            await self.send(chat, "正在重启 wxauto 模块，请稍候…")
            try:
                await self._worker.restart()
                await self.send(chat, "wxauto 模块已重启。")
            except Exception as exc:
                await self.send(chat, f"重启失败：{exc}")

        elif reply == "__ACTION__:statwx":
            stats = self._worker.stats()
            msg = (
                f"wxauto 状态报告\n"
                f"运行时长: {stats.uptime_seconds:.0f}s\n"
                f"监听聊天: {', '.join(stats.listen_chats) or '无'}\n"
                f"延迟范围: {stats.min_delay}~{stats.max_delay}s\n"
                f"已见消息ID数: {stats.seen_ids_count}\n"
                f"运行中: {'是' if stats.running else '否'}"
            )
            await self.send(chat, msg)

        elif reply == "__ACTION__:procstat":
            all_stats = self._proc_manager.get_stats()
            if not all_stats:
                await self.send(chat, "无注册的后台进程。")
            else:
                lines = ["后台进程状态："]
                for s in all_stats:
                    uptime = s.uptime_str if s.state == "running" else "-"
                    pid_str = str(s.pid) if s.pid is not None else "-"
                    lines.append(
                        f"  {s.name}: {s.state}  pid={pid_str}"
                        f"  重启={s.restart_count}  运行={uptime}"
                    )
                await self.send(chat, "\n".join(lines))

        elif reply.startswith("__ACTION__:parserdebug:"):
            await self.send(chat, reply[len("__ACTION__:parserdebug:"):])

        else:
            if reply:
                await self.send(chat, reply)

    # async def _notify_agent(self, narration: str, raw: dict) -> None:
    #     """将只读旁白事件以 internal MessageEvent 的形式送入网关。"""
    #     chat = raw.get("chat", "")
    #     entry = self._cfg.chat_entry(chat)
    #     chat_type = self._resolve_chat_type(raw, entry)
    #     source = self.build_source(
    #         chat_id=chat,
    #         chat_name=chat,
    #         chat_type=chat_type,
    #         user_id="__system__",
    #         user_name="系统",
    #     )
    #     event = MessageEvent(
    #         text=f"[旁白] {narration}",
    #         message_type=MessageType.TEXT,
    #         source=source,
    #         internal=True,
    #     )
    #     try:
    #         await self.handle_message(event)
    #     except Exception:
    #         # 旁白失败不应中断主流程，降级为日志记录
    #         logger.warning("[%s] 旁白事件投递失败，仅记录日志: %s", PLATFORM_NAME, narration)

    def _build_event(self, raw: dict) -> MessageEvent:
        """将通过过滤的原始消息组装为 Hermes MessageEvent。"""
        chat = raw["chat"]
        sender = raw["sender"]
        entry = self._cfg.chat_entry(chat)
        chat_type = self._resolve_chat_type(raw, entry)

        source = self.build_source(
            chat_id=chat,
            chat_name=chat,
            chat_type=chat_type,
            user_id=sender,
            user_name=sender,
            message_id=raw.get("id", ""),
        )
        return MessageEvent(
            text=raw.get("content", ""),
            message_type=MessageType.TEXT,
            source=source,
            message_id=raw.get("id", ""),
            raw_message=raw,
        )

    @staticmethod
    def _resolve_chat_type(raw: dict, entry) -> str:
        """从聊天表条目或 doc 规定的回退启发式规则推断聊天类型。"""
        if entry is not None:
            t = entry.type
            if t == "group":
                return "group"
            if t == "self":
                return "dm"
            return "dm"
        # 回退：self 类型视为私聊；否则 chat == sender 则私聊，不等则群聊
        if raw.get("type") == "self":
            return "dm"
        return "dm" if raw.get("chat") == raw.get("sender") else "group"


    async def _send_text(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        *,
        strip_md: bool = True,
    ) -> SendResult:
        """原子操作：向微信聊天发送纯文本消息。

        WeChat 不返回消息 ID，合成一个本地 UUID 供网关追踪。
        reply_to 在 SendMsg 中无原生支持，忽略。
        strip_md=True（默认）会在发送前剥除 Markdown 格式符。
        """
        if strip_md:
            content = strip_markdown(content)
        try:
            await self._worker.submit(self._worker._wx.SendMsg, content, who=chat_id)
            logger.debug("[%s] 发送消息成功 chat=%s content=%s", PLATFORM_NAME, chat_id, content)
            return SendResult(success=True, message_id=f"wx-{uuid.uuid4().hex[:12]}")
        except Exception as exc:
            logger.exception("[%s] 发送消息失败 chat=%s", PLATFORM_NAME, chat_id)
            return SendResult(success=False, error=str(exc))

    async def _send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        """原子操作：向微信聊天发送本地文件（图片、音频等均可）。

        使用 SendFiles 发送本地文件路径；若提供 caption 则在文件后追加文本消息。
        WeChat 不返回消息 ID，合成一个本地 UUID 供网关追踪。
        """
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"文件不存在: {file_path}")
        try:
            await self._worker.submit(self._worker._wx.SendFiles, str(path.resolve()), who=chat_id)
            logger.debug("[%s] 发送文件成功 chat=%s path=%s", PLATFORM_NAME, chat_id, file_path)
            if caption:
                await self._worker.submit(self._worker._wx.SendMsg, caption, who=chat_id)
            return SendResult(success=True, message_id=f"wx-{uuid.uuid4().hex[:12]}")
        except Exception as exc:
            logger.exception("[%s] 发送文件失败 chat=%s", PLATFORM_NAME, chat_id)
            return SendResult(success=False, error=str(exc))

    async def _directive_file(self, chat_id: str, attrs: dict[str, str]) -> SendResult:
        """<file url="..."/> 指令动作处理器。"""
        url = attrs.get("url")
        if not url:
            return SendResult(success=False, error="<file> 指令缺少 url")
        return await self._send_file(chat_id, url)

    # ── 出站 ──────────────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None
    ) -> SendResult:
        if not has_directive_syntax(content):
            return await self._send_text(chat_id, content, reply_to, metadata)

        results: list[SendResult] = []
        for seg in parse(content):
            if isinstance(seg, Text):
                stripped = seg.value.strip()
                if stripped:
                    results.append(await self._send_text(chat_id, stripped, reply_to, metadata))
            else:
                h = self._directives.get(seg.name)
                if h:
                    results.append(await h(chat_id, seg.attrs))

        if not results:
            return await self._send_text(chat_id, content, reply_to, metadata)

        success = all(r.success for r in results)
        error = next((r.error for r in results if not r.success), None)
        mid = next((r.message_id for r in results if r.success), f"wx-{uuid.uuid4().hex[:12]}")
        return SendResult(success=success, message_id=mid, error=error)

    async def send_typing(self, chat_id: str) -> None:
        """微信无输入状态 API，空操作。"""
        return None

    async def get_chat_info(self, chat_id: str) -> dict:
        entry = self._cfg.chat_entry(chat_id)
        if entry:
            type_map = {"group": "group", "self": "dm", "friend": "dm"}
            return {"name": entry.name, "type": type_map.get(entry.type, "dm")}
        return {"name": chat_id, "type": "dm"}


# ─────────────────────────────────────────────────────────────────────────────
# 注册帮助函数
# ─────────────────────────────────────────────────────────────────────────────

def check_requirements() -> bool:
    """快速检查：是否在 Windows 上"""
    import sys
    return sys.platform == "win32"


def validate_config(config: PlatformConfig) -> bool:
    """有配置文件或设置了 WX_AUTO_ENABLED 即视为有效。"""
    import os as _os
    from pathlib import Path
    if _os.getenv("WX_AUTO_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        return True
    extra = getattr(config, "extra", {}) or {}
    path = extra.get("config_path") or _os.getenv("WX_AUTO_CONFIG_PATH", "")
    default = Path(__file__).parent / "config.yaml"
    return bool(path and Path(path).exists()) or default.exists()


def _env_enablement() -> Optional[dict]:
    """当 WX_AUTO_ENABLED=true 或 config.json 存在时自动启用。"""
    import os as _os
    from pathlib import Path
    enabled = _os.getenv("WX_AUTO_ENABLED", "").strip().lower() in ("1", "true", "yes")
    config_json = Path(__file__).parent / "config.yaml"
    if not enabled and not config_json.exists():
        return None

    seed: dict[str, Any] = {}
    home = _os.getenv("WX_AUTO_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": "Home"}
    config_path = _os.getenv("WX_AUTO_CONFIG_PATH", "").strip()
    if config_path:
        seed["config_path"] = config_path
    return seed


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,  # noqa: ARG001
    force_document: bool = False,  # noqa: ARG001
) -> dict:
    """cron 投递用的临时连接发送器。"""
    adapter = WxautoPlatformAdapter(pconfig, bind_agent_tools=False)
    if not await adapter.connect():
        return {"error": "连接失败"}
    try:
        result = await adapter.send(chat_id, message, reply_to=thread_id)
        if result.success:
            return {"success": True, "message_id": result.message_id}
        return {"error": result.error or "发送失败"}
    finally:
        await adapter.disconnect()


# ─────────────────────────────────────────────────────────────────────────────
# Plugin 入口点
# ─────────────────────────────────────────────────────────────────────────────

def register(ctx) -> None:
    """Hermes 插件系统在启动时调用此函数。"""
    register_tools(ctx)
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="WX Auto",
        adapter_factory=lambda cfg: WxautoPlatformAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[],
        install_hint="pip install pywin32 comtypes pyperclip pillow psutil pyotp",
        env_enablement_fn=_env_enablement,
        standalone_sender_fn=_standalone_send,
        cron_deliver_env_var="WX_AUTO_HOME_CHANNEL",
        allowed_users_env="WX_AUTO_ALLOWED_USERS",
        allow_all_env="WX_AUTO_ALLOW_ALL_USERS",
        max_message_length=4000,
        platform_hint="""
            You are chatting via Wechat Automata Messaging API as a real person. 
            Wechat does NOT renderMarkdown — text bubbles show ** and # literally. 
            Bare URLs are auto-linked, but \\[label\\](url) syntax is not.
            Each text bubble is capped at 4000 characters. 
            Send image/audio/video files by referencing them with <file url="local_path"/> in your reply text.
        """,
        emoji="💬",
    )
