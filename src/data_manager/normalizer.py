"""wxauto 原始消息到统一消息模型的转换。"""

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional
from datetime import tzinfo

from data_manager.models import ParsedMessage
from data_manager.time_utils import normalize_iso, now_iso

_IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_AUDIO_EXTENSIONS = {".aac", ".amr", ".flac", ".m4a", ".mp3", ".ogg", ".silk", ".wav"}
_VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
_PLACEHOLDER_TYPES = {
    "[动画表情]": "animated_emoji",
    "[图片]": "image",
    "[语音]": "voice",
    "[视频]": "video",
    "[音乐]": "music",
    "[链接]": "link",
}


def _looks_like_path(content: str) -> bool:
    path = PureWindowsPath(content)
    return bool(path.drive and path.suffix) or (
        ("/" in content or "\\" in content) and bool(path.suffix)
    )


def _type_from_extension(path: str) -> str:
    suffix = PureWindowsPath(path).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _AUDIO_EXTENSIONS:
        return "voice"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    return "file"


def classify_content(
    content: str,
    path_exists: Callable[[str], bool] = lambda value: Path(value).is_file(),
) -> tuple[str, Optional[str], str]:
    if _looks_like_path(content):
        status = "available" if path_exists(content) else "missing"
        return _type_from_extension(content), content, status
    message_type = _PLACEHOLDER_TYPES.get(content)
    if message_type is not None:
        status = (
            "not_applicable"
            if message_type in {"animated_emoji", "music", "link"}
            else "capture_failed"
        )
        return message_type, None, status
    return "text", None, "not_applicable"


def contains_agent_mention(content: str, agent_names: list[str]) -> bool:
    for name in agent_names:
        pattern = rf"@\s*{re.escape(name)}(?=$|[\s,，。.!！?？:：;；])"
        if re.search(pattern, content):
            return True
    return False


def normalize_online_message(
    raw: dict,
    *,
    chat_type: str,
    timezone: tzinfo,
    agent_names: list[str],
) -> ParsedMessage:
    content = str(raw.get("content", ""))
    observed_at = normalize_iso(raw.get("timestamp") or now_iso(timezone), timezone)
    message_type, file_path, file_status = classify_content(content)
    msg_type = str(raw.get("type", "friend"))
    source_message_id = str(raw.get("id", "")).strip() or None
    return ParsedMessage(
        source_type="wxauto_online",
        source_message_id=source_message_id,
        sent_at=observed_at,
        sent_at_source="observed_fallback",
        observed_at=observed_at,
        chat_id=str(raw.get("chat", "")),
        chat_type=chat_type,
        sender_id=None,
        sender_name=str(raw.get("sender", "")),
        sender_remark=(
            str(raw["sender_remark"]) if raw.get("sender_remark") is not None else None
        ),
        direction="outgoing" if msg_type == "self" else "incoming",
        message_type=message_type,
        content=content,
        file_path=file_path,
        file_status=file_status,
        mentioned_agent=(
            chat_type == "group" and contains_agent_mention(content, agent_names)
        ),
        raw_json=json.dumps(raw, ensure_ascii=False, sort_keys=True),
    )
