"""带 IANA 时区的消息时间解析与序列化。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def resolve_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name != "Asia/Shanghai":
            logger.warning(
                "[wx-data] IANA 时区 %r 不可用，回退 Asia/Shanghai",
                name,
            )
            try:
                return ZoneInfo("Asia/Shanghai")
            except ZoneInfoNotFoundError:
                pass
        logger.warning(
            "[wx-data] 未安装 IANA 时区数据，临时使用固定 UTC+08:00；"
            "请安装 requirements.txt 中的 tzdata"
        )
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_iso(timezone_info: tzinfo) -> str:
    return datetime.now(timezone_info).isoformat(timespec="seconds")


def normalize_datetime(value: datetime | str, timezone_info: tzinfo) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_info)
    return parsed.astimezone(timezone_info)


def normalize_iso(value: datetime | str, timezone_info: tzinfo) -> str:
    return normalize_datetime(value, timezone_info).isoformat(timespec="seconds")
