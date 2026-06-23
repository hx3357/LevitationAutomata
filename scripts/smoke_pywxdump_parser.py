"""对真实微信数据库执行 PyWxDumpParser 冒烟测试。

PowerShell:
    $env:WX_AUTO_WECHAT_DB_KEY = "<64-hex-key>"
    python scripts/smoke_pywxdump_parser.py
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_manager.parsers import PyWxDumpParser
from data_manager.time_utils import resolve_timezone


DEFAULT_WX_PATH = Path(
    r"C:\Users\hx335\Documents\WeChat Files\wxid_fsyukhgumgr829"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wx-path", type=Path, default=DEFAULT_WX_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "test-output" / "pywxdump-smoke",
    )
    parser.add_argument("--limit", type=int, default=20)
    return parser.parse_args()


def choose_examples(
    parser: PyWxDumpParser,
    database: Any,
) -> tuple[str | None, str | None, str | None]:
    users = database.get_user() or {}
    user_id = next(
        (wxid for wxid in users if not str(wxid).endswith("@chatroom")),
        None,
    )
    group_id = next(
        (wxid for wxid in users if str(wxid).endswith("@chatroom")),
        None,
    )
    member_id = None
    if group_id:
        rooms = database.get_room_list(roomwxids=[group_id]) or {}
        room = rooms.get(group_id, {})
        members = room.get("wxid2userinfo", {})
        member_id = next(iter(members), None)
    return user_id, group_id, member_id


def choose_talker(database: Any) -> tuple[str, int]:
    counts = database.get_msgs_count() or {}
    candidates = [
        (int(count), str(wxid))
        for wxid, count in counts.items()
        if wxid != "total" and int(count) > 0
    ]
    if not candidates:
        raise RuntimeError("合并数据库中没有可供测试的聊天消息")
    candidates.sort(reverse=True)
    count, talker_id = candidates[0]
    return talker_id, count


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="backslashreplace")
    args = parse_args()
    key = os.getenv("WX_AUTO_WECHAT_DB_KEY", "").strip()
    if not key:
        raise RuntimeError("请先设置环境变量 WX_AUTO_WECHAT_DB_KEY")

    output = args.output.resolve()
    parser = PyWxDumpParser(
        wx_path=args.wx_path.resolve(),
        merge_path=output / "merge_all.db",
        media_cache_path=output / "media",
        chats={"__smoke__": "friend"},
        timezone=resolve_timezone("Asia/Shanghai"),
        page_size=max(1, args.limit),
        key=key,
    )

    print(f"[1/5] 解密并合并数据库: {args.wx_path}")
    merge_path = parser.refresh()
    print(f"      合并库: {merge_path}")

    database = parser._ensure_database()
    user_id, group_id, member_id = choose_examples(parser, database)

    print("[2/5] 名称查询")
    if user_id:
        print(f"      用户 {user_id}: {parser.get_user_nickname(user_id)}")
    if group_id:
        print(f"      群聊 {group_id}: {parser.get_group_name(group_id)}")
    if group_id and member_id:
        print(
            f"      群成员 {member_id}: "
            f"{parser.get_group_member_name(group_id, member_id)}"
        )

    talker_id, talker_count = choose_talker(database)
    scan_size = max(200, max(1, args.limit))
    raw_rows, _ = database.get_msgs(
        wxids=talker_id,
        start_index=max(0, talker_count - scan_size),
        page_size=scan_size,
    )
    if not raw_rows:
        raise RuntimeError(f"会话 {talker_id} 计数非零但无法读取消息")
    times = [
        datetime.fromisoformat(str(row["CreateTime"]))
        for row in raw_rows
        if row.get("CreateTime")
    ]
    start_at = min(times) - timedelta(seconds=1)
    end_at = max(times) + timedelta(seconds=1)

    users = database.get_user(wxids=[talker_id]) or {}
    talker = users.get(talker_id, {})
    chat_name = str(
        talker.get("remark")
        or talker.get("nickname")
        or talker_id
    )
    print(
        f"[3/5] 查询会话 {chat_name!r}，"
        f"时间段 {start_at} - {end_at}"
    )
    try:
        parsed_messages = parser.get_messages(chat_name, start_at, end_at)
    except ValueError:
        parsed_messages = parser.get_messages(talker_id, start_at, end_at)
    if not parsed_messages:
        raise RuntimeError("Parser 未返回任何 ParsedMessage")
    messages = parsed_messages[-max(1, args.limit) :]

    print(f"[4/5] ParsedMessage 数量: {len(messages)}")
    for message in messages[:5]:
        print(
            f"      {message.sent_at} {message.chat_id} "
            f"{message.sender_name} {message.message_type}: "
            f"{message.content}"
        )

    print("[5/5] 检查 available 媒体路径")
    checked = 0
    for message in parsed_messages:
        if message.file_status != "available":
            continue
        checked += 1
        if not message.file_path or not Path(message.file_path).is_file():
            raise RuntimeError(f"媒体路径不可用: {message.file_path}")
    print(f"      已检查 {checked} 个 available 媒体文件")
    print("PyWxDumpParser smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
