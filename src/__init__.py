"""wx-auto-platform · src 包

适配器内部模块集合。模块间以扁平路径互导（adapter.py 将 src/ 注入 sys.path）；
本文件同时支持 Hermes 插件根以包方式导入（from src.xxx import ...）。

公共 API：
    config          — WxAutoConfig、ChatEntry、load_config、save_config
    commands        — CommandRegistry、_registry（内置命令单例）
    filters         — apply_filters、FilterResult、FilterOutput
    wechat_worker   — WeChatWorker（COM 单线程 worker）
    throttled_wechat — ThrottledWeChat（随机延迟 WeChat 子类）
"""

from .config import (
    AdminEntry,
    BlacklistEntry,
    ChatEntry,
    WxAutoConfig,
    load_config,
    save_config,
)
from .commands.commands import CommandRegistry, _registry, execute_command, parse_command
from .filters import FilterOutput, FilterResult, apply_filters
from .throttled_wechat import ThrottledWeChat
from .wechat_worker import WeChatWorker, WorkerStats

__all__ = [
    # config
    "WxAutoConfig",
    "ChatEntry",
    "AdminEntry",
    "BlacklistEntry",
    "load_config",
    "save_config",
    # commands
    "CommandRegistry",
    "_registry",
    "execute_command",
    "parse_command",
    # filters
    "FilterResult",
    "FilterOutput",
    "apply_filters",
    # worker
    "WeChatWorker",
    "WorkerStats",
    # throttled wechat
    "ThrottledWeChat",
]
