"""命令系统 — 解析、TOTP 鉴权、内置命令注册。

命令格式：/<名称> [TOTP码] [位置参数...] [--选项 值]
    - 管理员命令的第一个参数固定为当前 TOTP 动态码（6 位数字）。
    - TOTP 验证使用 pyotp，允许相邻 ±1 个时间窗，并维护防重放缓存。
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from config import WxAutoConfig
    from filters import FilterOutput

logger = logging.getLogger(__name__)

# ── 命令解析 ──────────────────────────────────────────────────────────────────

_COMMAND_RE = re.compile(r"^/([a-zA-Z][a-zA-Z0-9_-]*)(.*)$", re.DOTALL)


def parse_command(text: str) -> Optional[dict]:
    """将 /cmd [args] 解析为结构化字典，失败返回 None。

    返回示例：
        {"name": "ban", "totp": "837461", "raw_args": "837461 网友塔顺菲",
         "pos": ["网友塔顺菲"], "opts": {}}
    """
    m = _COMMAND_RE.match(text.strip())
    if not m:
        return None
    # 过滤路径型命令（如 /etc/passwd）
    name = m.group(1).lower()
    raw_args = m.group(2).strip()

    tokens = raw_args.split() if raw_args else []
    opts: dict[str, str] = {}
    pos: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i].startswith("--") and i + 1 < len(tokens):
            opts[tokens[i][2:]] = tokens[i + 1]
            i += 2
        else:
            pos.append(tokens[i])
            i += 1

    return {
        "name": name,
        "raw_args": raw_args,
        "pos": pos,
        "opts": opts,
        # totp 是第一个位置参数（仅用于管理员命令；由 filters.py 提取）
        "totp": pos[0] if pos else "",
    }


# ── TOTP 防重放缓存 ────────────────────────────────────────────────────────────

class _ReplayCache:
    """TTL ≈ 60 秒（两个 30 秒窗口）的已用动态码缓存。"""

    def __init__(self, ttl: float = 60.0) -> None:
        self._ttl = ttl
        # code -> 过期时间戳
        self._cache: OrderedDict[str, float] = OrderedDict()

    def is_used(self, code: str) -> bool:
        self._evict()
        return code in self._cache

    def mark_used(self, code: str) -> None:
        self._evict()
        self._cache[code] = time.monotonic() + self._ttl

    def _evict(self) -> None:
        now = time.monotonic()
        keys = [k for k, exp in list(self._cache.items()) if exp <= now]
        for k in keys:
            del self._cache[k]


_replay_cache = _ReplayCache()


# ── 命令注册表 ────────────────────────────────────────────────────────────────

HandlerFn = Callable[[dict, dict, Any], Optional[str]]
# 签名：handler(parsed_cmd, raw_msg, cfg) -> reply_text or None


class CommandRegistry:
    """运行时命令注册表。"""

    def __init__(self) -> None:
        # name -> {handler, admin, description, usage}
        self._commands: dict[str, dict] = {}

    def register(
        self,
        name: str,
        handler: HandlerFn,
        admin: bool = False,
        description: str = "",
        usage: str = "",
    ) -> None:
        self._commands[name.lower()] = {
            "handler": handler,
            "admin": admin,
            "description": description,
            "usage": usage,
        }

    def get(self, name: str) -> Optional[dict]:
        return self._commands.get(name.lower())

    def all_commands(self) -> dict[str, dict]:
        return dict(self._commands)

    @staticmethod
    def verify_totp(secret: str, code: str) -> bool:
        """验证 TOTP 动态码（±1 窗口），并检查防重放缓存。"""
        if not secret or not code:
            return False
        if not re.fullmatch(r"\d{6}", code):
            return False
        if _replay_cache.is_used(code):
            logger.warning("[wx-auto] TOTP 重放攻击：code=%s", code)
            return False
        try:
            import pyotp  # type: ignore
            ok = pyotp.TOTP(secret).verify(code, valid_window=1)
        except Exception:
            logger.exception("[wx-auto] pyotp 验证异常")
            return False
        if ok:
            _replay_cache.mark_used(code)
        return ok


# ── 命令执行 ──────────────────────────────────────────────────────────────────

def execute_command(
    content: str,
    raw: dict,
    cfg: WxAutoConfig,
    registry: CommandRegistry,
    sender: str,
) -> FilterOutput:
    """解析并执行命令，返回 FilterOutput；未识别内容返回 PASS。"""
    from filters import FilterOutput, FilterResult  # noqa: PLC0415 — 避免循环导入

    parsed = parse_command(content)
    if parsed is None:
        return FilterOutput(result=FilterResult.PASS)

    cmd_name = parsed["name"]
    cmd_spec = registry.get(cmd_name)
    if cmd_spec is None:
        return FilterOutput(result=FilterResult.PASS)

    if cmd_spec["admin"]:
        chat = raw.get("chat", "")
        is_admin = cfg.is_admin(chat, sender)
        totp_ok = (
            is_admin
            and bool(cfg.totp_secret)
            and registry.verify_totp(cfg.totp_secret, parsed.get("totp", ""))
        )
        if not is_admin or not totp_ok:
            reason = "未命中管理员列表" if not is_admin else "TOTP 鉴权失败"
            logger.warning(
                "[wx-auto] 非法管理员命令: sender=%s cmd=/%s totp=%s reason=%s",
                sender, cmd_name, parsed.get("totp", ""), reason,
            )
            return FilterOutput(
                result=FilterResult.COMMAND_NARRATION,
                narration=f"{sender}试图使用非法指令 /{cmd_name}（{reason}）",
            )

    reply = cmd_spec["handler"](parsed, raw, cfg)
    prefix = "管理员" if cmd_spec["admin"] else ""
    return FilterOutput(
        result=FilterResult.COMMAND_NARRATION,
        narration=f"{sender}使用{prefix}指令 /{cmd_name}：{parsed.get('raw_args', '')}",
        command_reply=reply,
    )


# ── 内置命令处理函数 ──────────────────────────────────────────────────────────

def _handle_help(parsed: dict, raw: dict, cfg) -> str:
    lines = ["*可用命令：*"]
    for name, spec in sorted(_registry.all_commands().items()):
        prefix = "🔐 " if spec["admin"] else "   "
        usage = spec.get("usage") or f"/{name}"
        desc = spec.get("description", "")
        lines.append(f"{prefix}/{usage}  —  {desc}")
    lines.append("\n🔐 = 管理员命令（需 TOTP 验证）")
    return "\n".join(lines)


def _handle_restartwx(parsed: dict, raw: dict, cfg) -> str:
    # 实际 restart 需要 adapter 协调 worker；这里返回信号字符串，adapter 捕获后执行。
    # 约定：返回值以 "__ACTION__:" 开头表示特殊动作。
    return "__ACTION__:restartwx"


def _handle_statwx(parsed: dict, raw: dict, cfg) -> str:
    # worker stats 由 adapter 注入（此处返回 sentinel，adapter 替换为真实数据）
    return "__ACTION__:statwx"


def _handle_procstat(parsed: dict, raw: dict, cfg) -> str:
    # 实际统计数据由 adapter 持有 _proc_manager 注入；此处返回 sentinel。
    return "__ACTION__:procstat"


def _handle_parser_debug(parsed: dict, raw: dict, cfg) -> str:
    content = parsed.get("raw_args", "")
    if not content:
        return "用法：/parser_debug <待解析内容>"
    # adapter._dispatch_command_reply 捕获此前缀后调用 send()，触发完整解析链路。
    return "__ACTION__:parserdebug:" + content


def _handle_ban(parsed: dict, raw: dict, cfg) -> str:
    pos = parsed.get("pos", [])
    # 跳过第一个位置参数（TOTP 码）
    targets = pos[1:] if len(pos) > 1 else []
    if not targets:
        return "用法：/ban <TOTP码> <用户名>"
    chat = raw.get("chat", "*")
    added = []
    for uname in targets:
        if not cfg.is_blacklisted(chat, uname):
            cfg.add_to_blacklist(chat, uname)
            added.append(uname)
    if added:
        from config import save_config  # noqa: PLC0415
        save_config(cfg)
        return f"已将以下用户加入黑名单（{chat}）：" + "、".join(added)
    return "用户已在黑名单中，无需重复添加。"


# ── 全局注册表单例 ─────────────────────────────────────────────────────────────

_registry = CommandRegistry()

_registry.register(
    "help",
    _handle_help,
    admin=False,
    description="显示所有可用命令",
    usage="help",
)
_registry.register(
    "restartwx",
    _handle_restartwx,
    admin=True,
    description="重启 wxauto 模块（刷新 WeChat 实例）",
    usage="restartwx <TOTP码>",
)
_registry.register(
    "statwx",
    _handle_statwx,
    admin=False,
    description="查看 wxauto 模块状态和参数",
    usage="statwx",
)
_registry.register(
    "ban",
    _handle_ban,
    admin=True,
    description="将用户加入黑名单",
    usage="ban <TOTP码> <用户名>",
)
_registry.register(
    "procstat",
    _handle_procstat,
    admin=False,
    description="查看后台守护进程状态",
    usage="procstat",
)
_registry.register(
    "parser_debug",
    _handle_parser_debug,
    admin=False,
    description="调试消息解析器：按解析结果实际发送文字/文件",
    usage="parser_debug <待解析内容>",
)
