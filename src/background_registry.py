"""后台进程注册表 — 声明本插件需要常驻的所有守护子进程。

与 WxautoPlatformAdapter 解耦：adapter 只调用 register_all()，
具体注册哪些进程、用什么参数，全部在本文件维护。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from background_process import BackgroundProcessManager, ProcessSpec, RestartPolicy

if TYPE_CHECKING:
    from config import WxAutoConfig

logger = logging.getLogger(__name__)


def register_all(manager: BackgroundProcessManager, cfg: WxAutoConfig) -> None:
    """将所有守护子进程注册到 manager。

    在 WxautoPlatformAdapter.__init__ 中、start_all() 之前调用一次。
    新增进程时只需在本函数内追加 manager.register(...)，无需改动 adapter。
    """
    _register_camofox_browser(manager, cfg)


# ── 各进程的注册函数 ──────────────────────────────────────────────────────────

def _register_camofox_browser(manager: BackgroundProcessManager, cfg: WxAutoConfig) -> None:
    if not cfg.camofox_browser_path:
        return
    manager.register(ProcessSpec(
        name="camofox-browser",
        # Windows 上 npm 是批处理文件，必须用 npm.cmd 直接调用（不经 shell）
        command=["npm.cmd", "run", "start"],
        cwd=cfg.camofox_browser_path,
        log_enabled=False,
        log_path="C:\\Users\\hx335\\.hermes\\plugins\\wx-auto-platform\\src\\camofox_browser.log",
        # 浏览器服务进程需要常驻：任何退出（含正常）都重新拉起
        restart_policy=RestartPolicy.ALWAYS,
        restart_max_retries=5,
        # 浏览器冷启动较慢，指数退避基准适当放大
        restart_base_delay=3.0,
        # 给浏览器清理 socket / 临时文件的宽限时间
        stop_timeout=10.0,
    ))
    logger.debug("[bg-registry] 已注册 camofox-browser: cwd=%s", cfg.camofox_browser_path)
