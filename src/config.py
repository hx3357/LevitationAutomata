"""核心配置模块 — 聊天表、管理员、黑名单。

通过 config.yaml 加载结构化配置，环境变量（WX_AUTO_*）可覆写敏感字段。
依赖标准库 + PyYAML（hermes 环境已包含）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

logger = logging.getLogger(__name__)

# 默认路径：插件根目录下的 config.yaml（src/ 的上一级）
_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


@dataclass
class ChatEntry:
    """聊天表的单条记录。id 使用微信聊天名（唯一键）。"""
    id: str        # WeChat 聊天名（同时作为 chat_id 传给 Hermes）
    name: str      # 显示名（通常同 id）
    type: str      # "group" | "friend" | "self"


@dataclass
class AdminEntry:
    chat: str        # 该管理员所在的聊天名（"*" 表示全局）
    admin_name: str  # 管理员的 sender 显示名


@dataclass
class BlacklistEntry:
    chat: str       # 用户所在的聊天名（"*" 表示全局）
    user_name: str  # 被封禁的 sender 显示名


@dataclass
class ReconciliationConfig:
    enabled: bool = False
    interval_seconds: float = 600.0
    overlap_seconds: float = 120.0
    settle_delay_seconds: float = 30.0
    time_tolerance_seconds: float = 5.0


@dataclass
class PyWxDumpConfig:
    enabled: bool = False
    wx_path: str = ""
    merge_path: str = "data/pywxdump/merge_all.db"
    media_cache_path: str = "data/pywxdump/media"
    page_size: int = 500


@dataclass
class DataManagerConfig:
    enabled: bool = True
    database_path: str = "data/messages.db"
    timezone: str = "Asia/Shanghai"
    agent_names: list[str] = field(default_factory=lambda: ["Levi"])
    reconciliation: ReconciliationConfig = field(default_factory=ReconciliationConfig)
    pywxdump: PyWxDumpConfig = field(default_factory=PyWxDumpConfig)


@dataclass
class WxAutoConfig:
    # 聊天表
    chat_table: list[ChatEntry] = field(default_factory=list)
    # 管理员列表
    admins: list[AdminEntry] = field(default_factory=list)
    # 黑名单
    blacklist: list[BlacklistEntry] = field(default_factory=list)
    # TOTP 共享密钥（Base32），优先从环境变量读取
    totp_secret: str = ""
    # ThrottledWeChat 随机延迟范围（秒）
    min_delay: float = 0.5
    max_delay: float = 2.0
    # GetListenMessage 轮询间隔（秒）
    poll_interval: float = 0.5
    # Camofox 浏览器项目根目录（包含 package.json），空字符串表示不启动
    camofox_browser_path: str = ""
    # 本地消息事实库
    data_manager: DataManagerConfig = field(default_factory=DataManagerConfig)

    # ── 查询帮助方法 ──────────────────────────────────────────────────────────

    def chat_entry(self, chat_name: str) -> Optional[ChatEntry]:
        """按聊天名查找聊天表条目。"""
        for e in self.chat_table:
            if e.id == chat_name:
                return e
        return None

    def is_blacklisted(self, chat: str, sender: str) -> bool:
        for e in self.blacklist:
            if e.user_name == sender and (e.chat == chat or e.chat == "*"):
                return True
        return False

    def is_admin(self, chat: str, sender: str) -> bool:
        for e in self.admins:
            if e.admin_name == sender and (e.chat == chat or e.chat == "*"):
                return True
        return False

    def add_to_blacklist(self, chat: str, user_name: str) -> None:
        if not self.is_blacklisted(chat, user_name):
            self.blacklist.append(BlacklistEntry(chat=chat, user_name=user_name))


def load_config(path: Optional[str] = None) -> WxAutoConfig:
    """从 YAML 文件加载配置，然后用环境变量覆写敏感字段。"""
    config_path = Path(path or os.getenv("WX_AUTO_CONFIG_PATH", "") or _DEFAULT_CONFIG_PATH)

    raw: dict = {}
    if config_path.exists():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.exception("[wx-auto] 读取配置文件失败: %s", config_path)
    else:
        logger.warning("[wx-auto] 配置文件不存在，使用默认配置: %s", config_path)

    data_raw = raw.get("data_manager", {}) or {}
    reconciliation_raw = data_raw.get("reconciliation", {}) or {}
    pywxdump_raw = data_raw.get("pywxdump", {}) or {}
    timezone_name = str(data_raw.get("timezone", "Asia/Shanghai")).strip() or "Asia/Shanghai"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name == "Asia/Shanghai":
            logger.warning(
                "[wx-auto] 系统缺少 IANA 时区数据；DataManager 将暂用固定 UTC+08:00，"
                "请安装 tzdata"
            )
        else:
            logger.warning(
                "[wx-auto] IANA 时区 %r 不可用，回退为 Asia/Shanghai",
                timezone_name,
            )
            timezone_name = "Asia/Shanghai"

    cfg = WxAutoConfig(
        chat_table=[
            ChatEntry(id=str(e["id"]), name=str(e.get("name", e["id"])), type=e.get("type", "friend"))
            for e in raw.get("chat_table", [])
        ],
        admins=[
            AdminEntry(chat=str(e["chat"]), admin_name=str(e["admin_name"]))
            for e in raw.get("admins", [])
        ],
        blacklist=[
            BlacklistEntry(chat=str(e["chat"]), user_name=str(e["user_name"]))
            for e in raw.get("blacklist", [])
        ],
        totp_secret=str(raw.get("totp_secret", "")),
        min_delay=float(raw.get("min_delay", 0.5)),
        max_delay=float(raw.get("max_delay", 2.0)),
        poll_interval=float(raw.get("poll_interval", 0.5)),
        camofox_browser_path=str(raw.get("camofox_browser_path", "")),
        data_manager=DataManagerConfig(
            enabled=bool(data_raw.get("enabled", True)),
            database_path=str(data_raw.get("database_path", "data/messages.db")),
            timezone=timezone_name,
            agent_names=[
                str(name).strip()
                for name in data_raw.get("agent_names", ["Levi"])
                if str(name).strip()
            ],
            reconciliation=ReconciliationConfig(
                enabled=bool(reconciliation_raw.get("enabled", False)),
                interval_seconds=max(
                    1.0, float(reconciliation_raw.get("interval_seconds", 600.0))
                ),
                overlap_seconds=max(
                    0.0, float(reconciliation_raw.get("overlap_seconds", 120.0))
                ),
                settle_delay_seconds=max(
                    0.0, float(reconciliation_raw.get("settle_delay_seconds", 30.0))
                ),
                time_tolerance_seconds=max(
                    0.0, float(reconciliation_raw.get("time_tolerance_seconds", 5.0))
                ),
            ),
            pywxdump=PyWxDumpConfig(
                enabled=bool(pywxdump_raw.get("enabled", False)),
                wx_path=str(pywxdump_raw.get("wx_path", "")),
                merge_path=str(
                    pywxdump_raw.get(
                        "merge_path",
                        "data/pywxdump/merge_all.db",
                    )
                ),
                media_cache_path=str(
                    pywxdump_raw.get(
                        "media_cache_path",
                        "data/pywxdump/media",
                    )
                ),
                page_size=max(1, int(pywxdump_raw.get("page_size", 500))),
            ),
        ),
    )

    # 环境变量覆写 TOTP 密钥（避免明文写入配置文件）
    env_secret = os.getenv("WX_AUTO_ADMIN_TOTP_SECRET", "").strip()
    if env_secret:
        cfg.totp_secret = env_secret

    # 环境变量覆写 Camofox 路径（方便 CI / Docker 场景）
    env_camofox = os.getenv("WX_AUTO_CAMOFOX_PATH", "").strip()
    if env_camofox:
        cfg.camofox_browser_path = env_camofox

    return cfg


def save_config(cfg: WxAutoConfig, path: Optional[str] = None) -> None:
    """将配置序列化回 YAML（用于持久化黑名单更新等运行时变更）。"""
    config_path = Path(path or os.getenv("WX_AUTO_CONFIG_PATH", "") or _DEFAULT_CONFIG_PATH)
    data = {
        "chat_table": [{"id": e.id, "name": e.name, "type": e.type} for e in cfg.chat_table],
        "admins": [{"chat": e.chat, "admin_name": e.admin_name} for e in cfg.admins],
        "blacklist": [{"chat": e.chat, "user_name": e.user_name} for e in cfg.blacklist],
        # totp_secret 不写回文件；统一通过环境变量管理
        "min_delay": cfg.min_delay,
        "max_delay": cfg.max_delay,
        "poll_interval": cfg.poll_interval,
        "camofox_browser_path": cfg.camofox_browser_path,
        "data_manager": {
            "enabled": cfg.data_manager.enabled,
            "database_path": cfg.data_manager.database_path,
            "timezone": cfg.data_manager.timezone,
            "agent_names": list(cfg.data_manager.agent_names),
            "reconciliation": {
                "enabled": cfg.data_manager.reconciliation.enabled,
                "interval_seconds": cfg.data_manager.reconciliation.interval_seconds,
                "overlap_seconds": cfg.data_manager.reconciliation.overlap_seconds,
                "settle_delay_seconds": cfg.data_manager.reconciliation.settle_delay_seconds,
                "time_tolerance_seconds": (
                    cfg.data_manager.reconciliation.time_tolerance_seconds
                ),
            },
            "pywxdump": {
                "enabled": cfg.data_manager.pywxdump.enabled,
                "wx_path": cfg.data_manager.pywxdump.wx_path,
                "merge_path": cfg.data_manager.pywxdump.merge_path,
                "media_cache_path": cfg.data_manager.pywxdump.media_cache_path,
                "page_size": cfg.data_manager.pywxdump.page_size,
            },
        },
    }
    config_path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("[wx-auto] 配置已保存: %s", config_path)
