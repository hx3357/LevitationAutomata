"""消息指令解析模块 — 单次线性扫描，零外部依赖。

把含有形如 <name key="val"/> 自闭合标签的字符串解析为「文本段 / 指令段」序列。
本模块只负责语法解析与语义校验，不涉及任何发送逻辑（与 adapter 完全解耦）。

扩展新指令：
    1. 在 RECOGNIZED 中加一行：  "tag_name": _validate_fn
    2. 在 adapter 的 _directives 中加动作处理器。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


# ── 段类型 ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class Directive:
    name: str
    attrs: dict[str, str]


# ── 快路径检测 ────────────────────────────────────────────────────────────────

# 合法自闭合指令必含这五个字符；缺任一即无需进入解析器。
SYNTAX_CHARS = '<>="/'


def has_directive_syntax(text: str) -> bool:
    """文本中同时含有全部语法字符时返回 True；缺任一即 False（走快路径）。"""
    return all(c in text for c in SYNTAX_CHARS)


# ── url 校验（单独函数，供外部复用） ─────────────────────────────────────────

def is_existing_local_file(url: str) -> bool:
    """url 是合法本地路径且对应文件确实存在时返回 True。"""
    if not url or "://" in url:
        return False
    try:
        return Path(url).is_file()
    except OSError:
        return False


# ── 指令校验器（name -> callable(attrs) -> bool） ─────────────────────────────

def _validate_file(attrs: dict[str, str]) -> bool:
    return is_existing_local_file(attrs.get("url", ""))


# 已注册指令表：校验失败的 <...> 退化为普通文本，调用方无感知。
RECOGNIZED: dict[str, object] = {
    "file": _validate_file,
}


# ── 底层标签解析 ──────────────────────────────────────────────────────────────

def _match_tag(text: str, pos: int) -> Optional[tuple[str, dict[str, str], int]]:
    """尝试在 pos（指向 '<'）处解析一个自闭合标签。

    成功返回 (name, attrs, end)，end 指向标签结束后第一个字符。
    任何不合法的情况返回 None，调用方将 '<' 视为普通字符继续。
    """
    n = len(text)
    i = pos + 1  # 跳过 '<'

    # 1. 读标签名：首字符 [A-Za-z]，后续 [A-Za-z0-9_-]
    if i >= n or not (text[i].isalpha()):
        return None
    name_start = i
    i += 1
    while i < n and (text[i].isalnum() or text[i] in '_-'):
        i += 1
    name = text[name_start:i]

    attrs: dict[str, str] = {}

    # 2. 循环读属性 / 等待 />
    while i < n:
        # 跳空白
        while i < n and text[i] in ' \t\r\n':
            i += 1
        if i >= n:
            return None

        # 自闭合结尾 />
        if text[i] == '/' :
            if i + 1 < n and text[i + 1] == '>':
                return (name, attrs, i + 2)
            return None  # '/' 后不是 '>'

        # 普通 '>'（非自闭合）→ 不支持
        if text[i] == '>':
            return None

        # 属性名：[A-Za-z][A-Za-z0-9_-]*
        if not text[i].isalpha():
            return None
        key_start = i
        i += 1
        while i < n and (text[i].isalnum() or text[i] in '_-'):
            i += 1
        key = text[key_start:i]

        # 跳空白
        while i < n and text[i] in ' \t\r\n':
            i += 1
        if i >= n or text[i] != '=':
            return None
        i += 1  # 跳过 '='

        # 跳空白
        while i < n and text[i] in ' \t\r\n':
            i += 1
        if i >= n or text[i] not in ('"', "'"):
            return None
        quote = text[i]
        i += 1
        val_start = i
        while i < n and text[i] != quote:
            i += 1
        if i >= n:
            return None  # 引号未闭合
        val = text[val_start:i]
        i += 1  # 跳过闭合引号

        attrs[key] = val

    return None  # 到达字符串末尾未找到 />


# ── 公开解析 API ──────────────────────────────────────────────────────────────

def parse(
    text: str,
    recognized: Optional[dict] = None,
) -> Iterator[Text | Directive]:
    """单次线性扫描 text，惰性产出 Text / Directive 段序列。

    recognized: 指令校验器字典（name -> callable(attrs)->bool）；
                None 表示使用模块级 RECOGNIZED 默认表。
    未注册或校验失败的 <...> 当作普通字符处理，不报错。
    """
    if recognized is None:
        recognized = RECOGNIZED

    n = len(text)
    i = 0
    start = 0

    while True:
        lt = text.find('<', i)
        if lt == -1:
            break

        m = _match_tag(text, lt)
        if m is None:
            i = lt + 1
            continue

        name, attrs, end = m
        validator = recognized.get(name)
        if validator is None or not validator(attrs):
            i = lt + 1
            continue

        # 产出指令前的文本段（空串不产出）
        if lt > start:
            yield Text(text[start:lt])
        yield Directive(name, attrs)
        i = end
        start = end

    # 尾部剩余文本
    if start < n:
        yield Text(text[start:])
