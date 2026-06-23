from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_manager.parsers import PyWxDumpParser


TZ = timezone(timedelta(hours=8))


class FakeDatabase:
    def __init__(self, users: dict[str, dict[str, Any]] | None = None) -> None:
        self.users = users or {
            "wxid_me": {"nickname": "Me", "remark": ""},
            "wxid_friend": {"nickname": "Friend", "remark": "Alice"},
            "room@chatroom": {"nickname": "Team", "remark": ""},
            "wxid_member": {"nickname": "Bob", "remark": "Bobby"},
        }
        self.rooms = {
            "room@chatroom": {
                "wxid2userinfo": {
                    "wxid_member": {
                        "nickname": "Bob",
                        "remark": "Bobby",
                        "roomNickname": "Bob in Team",
                    }
                }
            }
        }
        self.messages: dict[str, list[dict[str, Any]]] = {
            "wxid_friend": [
                self._message(
                    1,
                    "101",
                    "文本",
                    "wxid_friend",
                    "hello",
                    "2026-06-22 10:00:00",
                ),
                self._message(
                    2,
                    "102",
                    "文件",
                    "wxid_me",
                    "document.txt",
                    "2026-06-22 10:01:00",
                    is_sender=1,
                    src=r"FileStorage\File\document.txt",
                ),
            ],
            "room@chatroom": [
                self._message(
                    3,
                    "103",
                    "图片",
                    "wxid_member",
                    "图片",
                    "2026-06-22 11:00:00",
                    src=r"FileStorage\Image\photo.dat",
                ),
                self._message(
                    4,
                    "104",
                    "语音",
                    "wxid_member",
                    "语音时长：1.00秒",
                    "2026-06-22 11:01:00",
                    src=r"room@chatroom\voice.wav",
                ),
                self._message(
                    5,
                    "105",
                    "不支持的类型",
                    "wxid_member",
                    "raw",
                    "2026-06-22 11:02:00",
                ),
            ],
        }
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    @staticmethod
    def _message(
        row_id: int,
        message_id: str,
        type_name: str,
        talker: str,
        msg: str,
        created_at: str,
        *,
        is_sender: int = 0,
        src: str = "",
    ) -> dict[str, Any]:
        return {
            "id": row_id,
            "MsgSvrID": message_id,
            "type_name": type_name,
            "is_sender": is_sender,
            "talker": talker,
            "room_name": "",
            "msg": msg,
            "src": src,
            "extra": {},
            "CreateTime": created_at,
        }

    def get_user(self, **_: Any) -> dict[str, dict[str, Any]]:
        return self.users

    def get_room_list(
        self,
        *,
        roomwxids: list[str],
    ) -> dict[str, dict[str, Any]]:
        return {
            room_id: self.rooms[room_id]
            for room_id in roomwxids
            if room_id in self.rooms
        }

    def get_msgs(self, **kwargs: Any) -> tuple[list[dict[str, Any]], dict]:
        self.calls.append(kwargs)
        rows = list(self.messages.get(str(kwargs["wxids"]), []))
        start = kwargs.get("start_createtime")
        end = kwargs.get("end_createtime")
        if start:
            rows = [
                row
                for row in rows
                if datetime.fromisoformat(row["CreateTime"]).replace(
                    tzinfo=TZ
                ).timestamp()
                >= start
            ]
        if end:
            rows = [
                row
                for row in rows
                if datetime.fromisoformat(row["CreateTime"]).replace(
                    tzinfo=TZ
                ).timestamp()
                <= end
            ]
        offset = int(kwargs["start_index"])
        limit = int(kwargs["page_size"])
        page = rows[offset : offset + limit]
        return page, {
            wxid: self.users[wxid]
            for wxid in {str(row["talker"]) for row in page}
            if wxid in self.users
        }

    def get_audio(self, _: str, **kwargs: Any) -> bytes:
        Path(kwargs["save_path"]).write_bytes(b"RIFFfake-wave")
        return b"RIFFfake-wave"

    def close(self) -> None:
        self.closed = True


class PyWxDumpParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wx_path = self.root / "wxid_me"
        (self.wx_path / "FileStorage" / "Image").mkdir(parents=True)
        (self.wx_path / "FileStorage" / "File").mkdir(parents=True)
        (self.wx_path / "FileStorage" / "Image" / "photo.dat").write_bytes(
            b"encrypted-image"
        )
        (self.wx_path / "FileStorage" / "File" / "document.txt").write_text(
            "hello",
            encoding="utf-8",
        )
        self.database = FakeDatabase()
        self.decrypt_calls = 0

        def decryptor(**kwargs: Any) -> tuple[bool, str]:
            self.decrypt_calls += 1
            destination = Path(kwargs["merge_save_path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"fake sqlite")
            return True, str(destination)

        self.parser = PyWxDumpParser(
            wx_path=self.wx_path,
            merge_path=self.root / "merge" / "all.db",
            media_cache_path=self.root / "media",
            chats={"Alice": "friend", "Team": "group"},
            timezone=TZ,
            agent_names=["Agent"],
            page_size=1,
            key="aa" * 32,
            decryptor=decryptor,
            db_factory=lambda *args, **kwargs: self.database,
            image_decoder=lambda _: (True, ".png", "image-md5", b"PNG"),
        )
        self.parser.refresh()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_name_lookups_and_group_member_priority(self) -> None:
        self.assertEqual(self.parser.get_user_nickname("wxid_friend"), "Friend")
        self.assertEqual(self.parser.get_group_name("room@chatroom"), "Team")
        self.assertEqual(
            self.parser.get_group_member_name(
                "room@chatroom",
                "wxid_member",
            ),
            "Bob in Team",
        )

    def test_exact_duplicate_name_is_rejected(self) -> None:
        duplicate_db = FakeDatabase(
            users={
                "wxid_one": {"nickname": "Duplicate", "remark": ""},
                "wxid_two": {"nickname": "Duplicate", "remark": ""},
            }
        )
        parser = PyWxDumpParser(
            wx_path=self.wx_path,
            merge_path=self.parser.merge_path,
            media_cache_path=self.root / "media-duplicate",
            chats={"Duplicate": "friend"},
            timezone=TZ,
            key="aa" * 32,
            decryptor=lambda **_: (True, str(self.parser.merge_path)),
            db_factory=lambda *args, **kwargs: duplicate_db,
            image_decoder=lambda _: (False, False, False, False),
        )
        with self.assertRaisesRegex(ValueError, "不唯一"):
            parser.get_messages("Duplicate")

    def test_private_messages_are_paginated_and_time_filtered(self) -> None:
        start = datetime(2026, 6, 22, 10, 0, 30, tzinfo=TZ)
        messages = self.parser.get_messages("Alice", start_at=start)
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message.direction, "outgoing")
        self.assertEqual(message.chat_id, "Alice")
        self.assertEqual(message.chat_type, "dm")
        self.assertEqual(message.sender_name, "Me")
        self.assertEqual(message.message_type, "file")
        self.assertEqual(message.file_status, "available")
        self.assertEqual(message.content, message.file_path)
        self.assertTrue(Path(message.file_path or "").is_file())
        self.assertGreaterEqual(len(self.database.calls), 2)

    def test_group_media_and_unknown_messages(self) -> None:
        messages = self.parser.get_messages("Team")
        self.assertEqual(
            [message.message_type for message in messages],
            ["image", "voice", "unknown"],
        )
        image, voice, unknown = messages
        self.assertEqual(image.sender_id, "wxid_member")
        self.assertEqual(image.sender_name, "Bob in Team")
        self.assertEqual(image.file_status, "available")
        self.assertTrue(Path(image.content or "").is_file())
        self.assertEqual(voice.file_status, "available")
        self.assertTrue(Path(voice.content or "").is_file())
        self.assertEqual(unknown.content, "raw")
        self.assertEqual(unknown.file_status, "not_applicable")

    def test_iter_messages_refreshes_once_for_all_configured_chats(self) -> None:
        before = self.decrypt_calls
        messages = list(self.parser.iter_messages(None, None))
        self.assertEqual(self.decrypt_calls, before + 1)
        self.assertEqual(len(messages), 5)

    def test_invalid_key_fails_validation(self) -> None:
        parser = PyWxDumpParser(
            wx_path=self.wx_path,
            merge_path=self.root / "invalid.db",
            media_cache_path=self.root / "invalid-media",
            chats=["Alice"],
            timezone=TZ,
            key="bad",
        )
        result = parser.validate_source()
        self.assertFalse(result.valid)
        self.assertIn("64", result.reason or "")


if __name__ == "__main__":
    unittest.main()
