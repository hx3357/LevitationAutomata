"""WX Auto — Hermes 平台适配器插件。

Hermes 插件系统在启动时会调用本包的 register(ctx) 完成注册。
实现细节见 adapter.py。
"""

from .adapter import PLATFORM_NAME, WxautoPlatformAdapter, register

__all__ = ["register", "WxautoPlatformAdapter", "PLATFORM_NAME"]
