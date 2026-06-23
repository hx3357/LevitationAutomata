"""微信数据库解析器策略与 PyWxDump 实现。"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Protocol

from data_manager.models import ParsedMessage, ValidationResult
from data_manager.normalizer import contains_agent_mention
from data_manager.time_utils import normalize_datetime, now_iso

logger = logging.getLogger(__name__)

_MESSAGE_TYPE_MAP = {
    "文本": "text",
    "粘贴的文本": "text",
    "引用回复": "text",
    "图片": "image",
    "语音": "voice",
    "视频": "video",
    "动画表情": "animated_emoji",
    "用户上传的GIF表情": "animated_emoji",
    "文件": "file",
    "文件(猜)": "file",
    "(分享)音乐": "music",
    "(分享)卡片式链接": "link",
}


class WeChatDatabaseParser(Protocol):
    @property
    def name(self) -> str: ...

    def validate_source(self) -> ValidationResult: ...

    def iter_messages(
        self,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> Iterable[ParsedMessage]: ...


class NullWeChatDatabaseParser:
    """未配置真实数据库源时使用的空解析器。"""

    @property
    def name(self) -> str:
        return "null"

    def validate_source(self) -> ValidationResult:
        return ValidationResult(False, "未配置真实微信数据库解析器")

    def iter_messages(
        self,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> Iterable[ParsedMessage]:
        logger.warning(
            "[wx-data] 使用空微信数据库解析器，离线消息补账未执行 "
            "start=%s end=%s",
            start_at,
            end_at,
        )
        return ()


@dataclass(frozen=True)
class _ResolvedChat:
    configured_name: str
    database_id: str
    chat_type: str


def _load_local_pywxdump() -> tuple[Any, Any, Any]:
    """按 vendored 项目的原始顶级包布局惰性加载依赖。"""
    vendor_parent = str(Path(__file__).resolve().parent)
    if vendor_parent not in sys.path:
        sys.path.insert(0, vendor_parent)
    package = importlib.import_module("pywxdump")
    db_module = importlib.import_module("pywxdump.db")
    utils_module = importlib.import_module("pywxdump.db.utils")
    return package.decrypt_merge, db_module.DBHandler, utils_module.dat2img


class PyWxDumpParser:
    """使用 vendored PyWxDump 读取微信 3.9.x 解密合并库。"""

    def __init__(
        self,
        *,
        wx_path: Path,
        merge_path: Path,
        media_cache_path: Path,
        chats: Mapping[str, str] | Sequence[str],
        timezone: tzinfo,
        agent_names: Sequence[str] = (),
        page_size: int = 500,
        key: str | None = None,
        decryptor: Callable[..., tuple[bool, str]] | None = None,
        db_factory: Callable[..., Any] | None = None,
        image_decoder: Callable[[str], tuple[Any, Any, Any, Any]] | None = None,
    ) -> None:
        self._wx_path = Path(wx_path)
        self._merge_path = Path(merge_path)
        self._media_cache_path = Path(media_cache_path)
        if isinstance(chats, Mapping):
            self._chats = {
                str(name): ("group" if kind == "group" else "dm")
                for name, kind in chats.items()
            }
        else:
            self._chats = {str(name): "dm" for name in chats}
        self._timezone = timezone
        self._agent_names = [str(name) for name in agent_names if str(name)]
        self._page_size = max(1, int(page_size))
        self._key = (key or os.getenv("WX_AUTO_WECHAT_DB_KEY", "")).strip()
        self._decryptor = decryptor
        self._db_factory = db_factory
        self._image_decoder = image_decoder
        self._db: Any | None = None
        self._db_pool_key: str | None = None
        self._users: dict[str, dict[str, Any]] | None = None
        self._rooms: dict[str, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "pywxdump"

    @property
    def merge_path(self) -> Path:
        return self._merge_path

    def validate_source(self) -> ValidationResult:
        if not self._wx_path.is_dir():
            return ValidationResult(
                False,
                f"微信数据目录不存在: {self._wx_path}",
            )
        if len(self._key) != 64:
            return ValidationResult(
                False,
                "环境变量 WX_AUTO_WECHAT_DB_KEY 必须是 64 位十六进制密钥",
            )
        try:
            bytes.fromhex(self._key)
        except ValueError:
            return ValidationResult(
                False,
                "环境变量 WX_AUTO_WECHAT_DB_KEY 不是有效十六进制密钥",
            )
        if not self._chats:
            return ValidationResult(False, "chat_table 为空，没有可同步的聊天")
        return ValidationResult(True)

    def refresh(self) -> Path:
        """解密核心数据库并增量合并到固定快照。"""
        validation = self.validate_source()
        if not validation.valid:
            raise ValueError(validation.reason)
        self._load_dependencies()
        assert self._decryptor is not None
        self._close_database()
        self._merge_path.parent.mkdir(parents=True, exist_ok=True)
        work_path = self._merge_path.parent / "decrypt_work"
        work_path.mkdir(parents=True, exist_ok=True)
        code, result = self._decryptor(
            wx_path=str(self._wx_path),
            key=self._key,
            outpath=str(work_path),
            merge_save_path=str(self._merge_path),
        )
        if not code:
            raise RuntimeError(f"微信数据库解密合并失败: {result}")
        result_path = Path(result)
        if not result_path.is_file():
            raise RuntimeError(f"解密合并未生成数据库: {result_path}")
        self._reset_caches()
        return result_path

    def get_user_nickname(self, wxid: str) -> str:
        user = self._get_users().get(wxid)
        if user is None:
            raise KeyError(f"未找到微信用户: {wxid}")
        return str(user.get("nickname") or wxid)

    def get_group_name(self, group_id: str) -> str:
        user = self._get_users().get(group_id)
        if user is None or not group_id.endswith("@chatroom"):
            raise KeyError(f"未找到微信群聊: {group_id}")
        return self._display_name(user, group_id)

    def get_group_member_name(self, group_id: str, wxid: str) -> str:
        room = self._get_room(group_id)
        members = room.get("wxid2userinfo", {}) if room else {}
        member = members.get(wxid)
        if not isinstance(member, dict):
            member = self._get_users().get(wxid, {})
        return self._member_display_name(member, wxid)

    def get_messages(
        self,
        chat_name: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> list[ParsedMessage]:
        self._ensure_database()
        resolved = self._resolve_chat(chat_name, self._chats.get(chat_name))
        return list(self._iter_chat_messages(resolved, start_at, end_at))

    def iter_messages(
        self,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> Iterable[ParsedMessage]:
        validation = self.validate_source()
        if not validation.valid:
            logger.warning("[wx-data] PyWxDump 源不可用: %s", validation.reason)
            return
        self.refresh()
        for chat_name, chat_type in self._chats.items():
            resolved = self._resolve_chat(chat_name, chat_type)
            yield from self._iter_chat_messages(resolved, start_at, end_at)

    def _load_dependencies(self) -> None:
        if (
            self._decryptor is not None
            and self._db_factory is not None
            and self._image_decoder is not None
        ):
            return
        decryptor, db_factory, image_decoder = _load_local_pywxdump()
        self._decryptor = self._decryptor or decryptor
        self._db_factory = self._db_factory or db_factory
        self._image_decoder = self._image_decoder or image_decoder

    def _ensure_database(self) -> Any:
        if self._db is not None:
            return self._db
        if not self._merge_path.is_file():
            self.refresh()
        self._load_dependencies()
        assert self._db_factory is not None
        self._db_pool_key = f"wx-auto-{uuid.uuid4().hex}"
        self._db = self._db_factory(
            {
                "key": self._db_pool_key,
                "type": "sqlite",
                "path": str(self._merge_path),
            },
            my_wxid=self._wx_path.name,
        )
        return self._db

    def _close_database(self) -> None:
        database = self._db
        pool_key = self._db_pool_key
        self._db = None
        self._db_pool_key = None
        if database is None:
            return
        try:
            database.close()
        except Exception:
            logger.debug("[wx-data] 关闭 PyWxDump 数据库连接失败", exc_info=True)
        pool_map = getattr(type(database), "_db_pool", None)
        if isinstance(pool_map, dict) and pool_key:
            pool_map.pop(pool_key, None)

    def _reset_caches(self) -> None:
        self._users = None
        self._rooms.clear()

    def _get_users(self) -> dict[str, dict[str, Any]]:
        if self._users is None:
            users = self._ensure_database().get_user()
            self._users = users if isinstance(users, dict) else {}
        return self._users

    def _get_room(self, group_id: str) -> dict[str, Any]:
        if group_id not in self._rooms:
            rooms = self._ensure_database().get_room_list(roomwxids=[group_id])
            room = rooms.get(group_id, {}) if isinstance(rooms, dict) else {}
            self._rooms[group_id] = room
        return self._rooms[group_id]

    def _resolve_chat(
        self,
        chat_name: str,
        configured_type: str | None,
    ) -> _ResolvedChat:
        users = self._get_users()
        matches: dict[str, dict[str, Any]] = {}
        for wxid, user in users.items():
            values = {
                str(wxid),
                str(user.get("nickname") or ""),
                str(user.get("remark") or ""),
            }
            if chat_name in values:
                matches[str(wxid)] = user
        if not matches:
            raise KeyError(f"未找到聊天名或 wxid: {chat_name}")
        if len(matches) > 1:
            candidates = ", ".join(
                f"{wxid}({self._display_name(user, wxid)})"
                for wxid, user in sorted(matches.items())
            )
            raise ValueError(f"聊天名 {chat_name!r} 不唯一，候选: {candidates}")
        database_id = next(iter(matches))
        inferred_type = "group" if database_id.endswith("@chatroom") else "dm"
        chat_type = configured_type or inferred_type
        if configured_type is not None and chat_type != inferred_type:
            raise ValueError(
                f"聊天 {chat_name!r} 的配置类型 {chat_type} 与数据库类型 "
                f"{inferred_type} 不一致: {database_id}"
            )
        return _ResolvedChat(chat_name, database_id, chat_type)

    def _iter_chat_messages(
        self,
        chat: _ResolvedChat,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> Iterable[ParsedMessage]:
        database = self._ensure_database()
        start_timestamp = self._to_timestamp(start_at)
        end_timestamp = self._to_timestamp(end_at)
        offset = 0
        while True:
            rows, users = database.get_msgs(
                wxids=chat.database_id,
                start_index=offset,
                page_size=self._page_size,
                start_createtime=start_timestamp,
                end_createtime=end_timestamp,
            )
            rows = rows or []
            page_users = users if isinstance(users, dict) else {}
            for raw in rows:
                yield self._convert_message(chat, raw, page_users)
            if len(rows) < self._page_size:
                break
            offset += len(rows)

    def _convert_message(
        self,
        chat: _ResolvedChat,
        raw: dict[str, Any],
        page_users: Mapping[str, dict[str, Any]],
    ) -> ParsedMessage:
        is_sender = int(raw.get("is_sender") or 0) == 1
        raw_talker = str(raw.get("talker") or "")
        sender_id = self._wx_path.name if is_sender else raw_talker
        if not sender_id or sender_id == "未知":
            sender_id = None

        user = page_users.get(sender_id or "", {})
        if not user and sender_id:
            user = self._get_users().get(sender_id, {})
        if chat.chat_type == "group" and not is_sender and sender_id:
            sender_name = self.get_group_member_name(chat.database_id, sender_id)
        else:
            sender_name = self._display_name(user, sender_id or "未知")
        sender_remark = str(user.get("remark") or "") or None

        type_name = str(raw.get("type_name") or "")
        message_type = _MESSAGE_TYPE_MAP.get(type_name, "unknown")
        original_content = str(raw.get("msg") or "")
        source = raw.get("src")
        content, file_path, file_status = self._materialize_media(
            message_type,
            source,
            raw,
            original_content,
        )
        sent_at = normalize_datetime(
            str(raw.get("CreateTime") or ""),
            self._timezone,
        ).isoformat(timespec="seconds")
        source_id = str(raw.get("MsgSvrID") or "").strip()
        if not source_id or source_id == "0":
            source_id = (
                f"{chat.database_id}:{raw.get('id', '')}:"
                f"{raw.get('CreateTime', '')}"
            )
        raw_payload = dict(raw)
        raw_payload["database_chat_id"] = chat.database_id
        raw_payload["configured_chat_id"] = chat.configured_name

        return ParsedMessage(
            source_type="wechat_database",
            source_message_id=source_id,
            sent_at=sent_at,
            sent_at_source="wechat_database",
            observed_at=sent_at,
            chat_id=chat.configured_name,
            chat_type=chat.chat_type,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_remark=sender_remark,
            direction="outgoing" if is_sender else "incoming",
            message_type=message_type,
            content=content,
            file_path=file_path,
            file_status=file_status,
            mentioned_agent=(
                chat.chat_type == "group"
                and message_type == "text"
                and contains_agent_mention(content or "", self._agent_names)
            ),
            raw_json=json.dumps(
                raw_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )

    def _materialize_media(
        self,
        message_type: str,
        source: Any,
        raw: Mapping[str, Any],
        original_content: str,
    ) -> tuple[str | None, str | None, str]:
        if message_type == "image":
            return self._materialize_image(str(source or ""))
        if message_type == "voice":
            return self._materialize_voice(raw)
        if message_type in {"file", "video"}:
            return self._materialize_existing_file(str(source or ""))
        return original_content or None, None, "not_applicable"

    def _materialize_image(
        self,
        relative_path: str,
    ) -> tuple[str | None, str | None, str]:
        source_path = self._source_path(relative_path)
        if source_path is None:
            return None, None, "missing"
        source_value = str(source_path.resolve())
        if not source_path.is_file():
            return source_value, source_value, "missing"
        self._load_dependencies()
        assert self._image_decoder is not None
        try:
            ok, extension, digest, output = self._image_decoder(str(source_path))
            if not ok:
                return source_value, source_value, "capture_failed"
            destination = self._media_cache_path / "images" / (
                f"{digest}{str(extension).lower()}"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                destination.write_bytes(bytes(output))
            value = str(destination.resolve())
            return value, value, "available"
        except Exception:
            logger.warning(
                "[wx-data] 图片解码失败: %s",
                source_path,
                exc_info=True,
            )
            return source_value, source_value, "capture_failed"

    def _materialize_voice(
        self,
        raw: Mapping[str, Any],
    ) -> tuple[str | None, str | None, str]:
        message_id = str(raw.get("MsgSvrID") or "").strip()
        if not message_id:
            return None, None, "capture_failed"
        destination = self._media_cache_path / "audio" / f"{message_id}.wav"
        value = str(destination.resolve())
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            return value, value, "available"
        try:
            result = self._ensure_database().get_audio(
                message_id,
                is_play=False,
                is_wave=True,
                save_path=str(destination),
                rate=24000,
            )
            if result and destination.is_file():
                return value, value, "available"
        except Exception:
            logger.warning(
                "[wx-data] 语音导出失败: %s",
                message_id,
                exc_info=True,
            )
        return value, value, "capture_failed"

    def _materialize_existing_file(
        self,
        relative_path: str,
    ) -> tuple[str | None, str | None, str]:
        source_path = self._source_path(relative_path)
        if source_path is None:
            return None, None, "missing"
        value = str(source_path.resolve())
        if source_path.is_file():
            return value, value, "available"
        return value, value, "missing"

    def _source_path(self, value: str) -> Path | None:
        value = value.strip().replace("\\\\", "\\")
        if not value or value.startswith(("http://", "https://")):
            return None
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
        return self._wx_path / Path(value.replace("\\", os.sep))

    def _to_timestamp(self, value: datetime | None) -> int | None:
        if value is None:
            return None
        return int(normalize_datetime(value, self._timezone).timestamp())

    @staticmethod
    def _display_name(user: Mapping[str, Any], fallback: str) -> str:
        return str(
            user.get("remark")
            or user.get("nickname")
            or user.get("roomNickname")
            or fallback
        )

    @staticmethod
    def _member_display_name(user: Mapping[str, Any], fallback: str) -> str:
        return str(
            user.get("roomNickname")
            or user.get("remark")
            or user.get("nickname")
            or fallback
        )

    def __del__(self) -> None:
        self._close_database()
