"""消息过滤管道 — 各步骤解耦，按序执行。

过滤逻辑（参考 docs/wx_platform.md §filter系统）：
    1. 黑名单检查：sender 命中 → 丢弃
    2. 聊天表范围检查：chat 未命中 → 丢弃
    3. 命令分支（仅当消息可解析为 /cmd 时）：
        - 管理员命令：list命中 + TOTP 验证 → 执行；否则通知旁白
        - 普通命令：执行处理函数，通知旁白
    非命令消息通过 → 返回 PASS，交由 adapter 组装 MessageEvent
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from config import WxAutoConfig
from commands import CommandRegistry, execute_command

logger = logging.getLogger(__name__)


class FilterResult(Enum):
    PASS = auto()            # 消息可进入 agent
    DROP = auto()            # 丢弃，无需进一步处理
    COMMAND_HANDLED = auto() # 命令已处理（旁白已生成），不进 agent
    COMMAND_NARRATION = auto()# 旁白事件，需转交 adapter 注入 agent


@dataclass
class FilterOutput:
    result: FilterResult
    # 当 result == COMMAND_NARRATION 时，携带旁白文本（只读事件）
    narration: Optional[str] = None
    # 命令处理器返回值（供 adapter 发送回 WeChat）
    command_reply: Optional[str] = None


def apply_filters(
    raw: dict,
    cfg: WxAutoConfig,
    registry: CommandRegistry,
) -> FilterOutput:
    """对单条原始消息执行完整过滤管道。
    注：自己发送的消息留下的消息历史仍然会进入过滤器，应丢弃。

    Args:
        raw: helloworld.py 格式的消息字典
        cfg: 当前配置
        registry: 命令注册表（含 TOTP 鉴权逻辑）
    """
    chat: str = raw.get("chat", "")
    sender: str = raw.get("sender", "")
    content: str = raw.get("content", "")
    msg_type: str = raw.get("type", "")

    # 只处理 friend 类型
    if msg_type != "friend":
        logger.debug("[wx-auto] 丢弃非 friend 消息 type=%s chat=%s", msg_type, chat)
        return FilterOutput(result=FilterResult.DROP)

    # 1. 黑名单
    if cfg.is_blacklisted(chat, sender):
        logger.warning("[wx-auto] 黑名单丢弃: sender=%s chat=%s", sender, chat)
        return FilterOutput(result=FilterResult.DROP)

    # 2. 聊天表范围
    chat_entry = cfg.chat_entry(chat)
    if chat_entry is None:
        logger.warning("[wx-auto] 聊天表未命中，丢弃: chat=%s sender=%s", chat, sender)
        return FilterOutput(result=FilterResult.DROP)

    # 3. 命令分支
    if content.startswith("/"):
        return execute_command(content, raw, cfg, registry, sender)

    # 非命令，通过
    return FilterOutput(result=FilterResult.PASS)
