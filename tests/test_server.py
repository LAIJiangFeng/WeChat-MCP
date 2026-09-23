import asyncio
import hashlib
import sqlite3
import tempfile
import os
import unittest
from unittest import mock
from contextlib import closing
from pathlib import Path

import zstandard

from wechat_mcp.server import WeChatBackup, mcp


def _table(username: str) -> str:
    digest = hashlib.md5(username.encode(), usedforsecurity=False).hexdigest()
    return f"Msg_{digest}"


def _create_message_db(path: Path, messages: dict[str, list[tuple]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        users = ("wxid_alice", "wxid_me", "team@chatroom", "wxid_bob")
        conn.executemany(
            "INSERT INTO Name2Id(rowid, user_name) VALUES (?, ?)", enumerate(users, 1)
        )
        for username, rows in messages.items():
            table = _table(username)
            conn.execute(
                f"CREATE TABLE [{table}] ("
                "local_id INTEGER PRIMARY KEY, local_type INTEGER, create_time INTEGER, "
                "real_sender_id INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER)"
            )
            conn.executemany(
                f"INSERT INTO [{table}] VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        conn.commit()


class WeChatBackupTest(unittest.TestCase):
    def test_read_only_backup_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contact_path = root / "contact" / "contact.db"
            contact_path.parent.mkdir(parents=True)
            with closing(sqlite3.connect(contact_path)) as conn:
                conn.execute(
                    "CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, alias TEXT)"
                )
                conn.executemany(
                    "INSERT INTO contact VALUES (?, ?, ?, ?)",
                    [
                        ("wxid_alice", "Alice", "项目小王", ""),
                        ("wxid_bob", "Bob", "", ""),
                        ("team@chatroom", "项目群", "", ""),
                    ],
                )
                conn.commit()

            compressed = zstandard.ZstdCompressor().compress("收到，明天提交".encode())
            _create_message_db(
                root / "message" / "message_0.db",
                {
                    "wxid_alice": [
                        (1, 1, 100, 1, "项目开始", 0),
                        (2, 1, 200, 2, compressed, 4),
                    ],
                    "team@chatroom": [
                        (1, 1, 250, 4, "wxid_bob:\n群里提到报销", 0),
                    ],
                },
            )
            _create_message_db(
                root / "message" / "message_1.db",
                {"wxid_alice": [(3, 1, 300, 1, "最终完成", 0)]},
            )

            backup = WeChatBackup(root)
            contacts = backup.list_contacts("小王")
            self.assertEqual(contacts[0]["name"], "项目小王")

            conversations = backup.list_conversations()
            alice = next(item for item in conversations if item["name"] == "项目小王")
            self.assertEqual(alice["message_count"], 3)

            history = backup.read_chat_history(alice["conversation"], limit=2)
            self.assertEqual(
                [item["content"] for item in history], ["收到，明天提交", "最终完成"]
            )
            self.assertEqual([item["sender"] for item in history], ["我", "项目小王"])

            matches = backup.search_messages("报销")
            self.assertEqual(matches[0]["conversation_name"], "项目群")
            self.assertEqual(matches[0]["sender"], "Bob")

            sanitized = WeChatBackup(root, sanitize=True)
            self.assertNotIn(
                "wxid_alice", sanitized.list_contacts("小王")[0]["username"]
            )
            self.assertEqual(
                len(sanitized.read_chat_history(alice["conversation"], limit=2)),
                2,
            )

            tool_names = {tool.name for tool in asyncio.run(mcp.list_tools())}
            self.assertEqual(
                tool_names,
                {
                    "list_conversations",
                    "list_contacts",
                    "read_chat_history",
                    "search_messages",
                    "analyze_message",
                },
            )
            prompt_names = {prompt.name for prompt in asyncio.run(mcp.list_prompts())}
            self.assertIn("empathetic_reply", prompt_names)

    def test_analyze_message_tool_and_prompt(self) -> None:
        # Pin the offline backend: this test asserts the tool wiring, not Jev,
        # and must not depend on the network or spend API quota.
        self.enterContext(mock.patch.dict(os.environ, {"EMPATHY_BACKEND": "rules"}))
        result = asyncio.run(
            mcp.call_tool(
                "analyze_message",
                {"message": "最近工作真的烦死了，感觉什么都做不好。", "relation": "朋友"},
            )
        )
        structured = result[1] if isinstance(result, tuple) else result
        self.assertEqual(structured["intent"], "vent")
        self.assertEqual(structured["relation"], "friend")
        self.assertIn("reply_strategy", structured)

        prompt = asyncio.run(
            mcp.get_prompt(
                "empathetic_reply",
                {"message": "算了，没事", "relation": "恋人", "context": ""},
            )
        )
        text = prompt.messages[0].content.text
        self.assertIn("先理解情绪", text)
        self.assertIn("恋人", text)
        self.assertIn("别矫情", text)


if __name__ == "__main__":
    unittest.main()
