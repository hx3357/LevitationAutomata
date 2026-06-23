"""Markdown 格式符剥离工具。

微信不渲染 Markdown，** 和 # 会原样显示，故在发送前将其剥除。
纯 re 实现，零外部依赖。
"""

import re

_RE_CODE_BLOCK   = re.compile(r'```[^\n]*\n?(.*?)(?:```|$)', re.DOTALL)
_RE_INLINE_CODE  = re.compile(r'`([^`\n]+)`')
_RE_HEADING      = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_RE_BOLD_ITALIC  = re.compile(r'\*{3}(.+?)\*{3}', re.DOTALL)
_RE_BOLD         = re.compile(r'\*{2}(.+?)\*{2}', re.DOTALL)
_RE_ITALIC_STAR  = re.compile(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', re.DOTALL)
_RE_BOLD_UNDER   = re.compile(r'__(.+?)__', re.DOTALL)
_RE_ITALIC_UNDER = re.compile(r'(?<!\w)_(.+?)_(?!\w)', re.DOTALL)
_RE_STRIKE       = re.compile(r'~~(.+?)~~', re.DOTALL)
_RE_IMAGE        = re.compile(r'!\[([^\]]*)\]\([^\)]*\)')
_RE_LINK         = re.compile(r'\[([^\]]+)\]\([^\)]*\)')
_RE_BLOCKQUOTE   = re.compile(r'^>\s?', re.MULTILINE)
_RE_HR           = re.compile(r'^[-*_]{3,}\s*$', re.MULTILINE)
_RE_UL           = re.compile(r'^(\s*)[-*+]\s+', re.MULTILINE)
_RE_OL           = re.compile(r'^(\s*)\d+\.\s+', re.MULTILINE)


def strip_markdown(text: str) -> str:
    """剥离 Markdown 格式符，返回适合微信纯文本气泡的内容。"""
    text = _RE_CODE_BLOCK.sub(lambda m: m.group(1).strip(), text)
    text = _RE_INLINE_CODE.sub(r'\1', text)
    text = _RE_HEADING.sub('', text)
    text = _RE_BOLD_ITALIC.sub(r'\1', text)
    text = _RE_BOLD.sub(r'\1', text)
    text = _RE_ITALIC_STAR.sub(r'\1', text)
    text = _RE_BOLD_UNDER.sub(r'\1', text)
    text = _RE_ITALIC_UNDER.sub(r'\1', text)
    text = _RE_STRIKE.sub(r'\1', text)
    text = _RE_IMAGE.sub(r'\1', text)
    text = _RE_LINK.sub(r'\1', text)
    text = _RE_BLOCKQUOTE.sub('', text)
    text = _RE_HR.sub('', text)
    text = _RE_UL.sub(r'\1', text)
    text = _RE_OL.sub(r'\1', text)
    return text
