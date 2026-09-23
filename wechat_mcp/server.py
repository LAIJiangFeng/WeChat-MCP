from __future__ import annotations

import hashlib
import html
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property, lru_cache
from itertools import groupby
from pathlib import Path
from typing import Any

import zstandard
from mcp.server.fastmcp import FastMCP

from wechat_mcp import empathy, jev

_MSG_DB_RE = re.compile(r"message_(\d+)\.db", re.IGNORECASE)
_MSG_TABLE_RE = re.compile(r"Msg_[0-9a-f]{32}", re.IGNORECASE)
_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GROUP_SENDER_RE = re.compile(r"[^\s<>:]{1,128}")
_XML_TAGS = ("title", "des", "filename", "url")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_MAX_LIMIT = 200
_MAX_QUERY_LENGTH = 200
_MAX_CONTENT_LENGTH = 1_000

_MESSAGE_TYPES = {
    1: "文本",
    3: "图片",
    34: "语音",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "链接/文件",
    50: "通话",
    10000: "系统",
    10002: "撤回",
}

_ZSTD = zstandard.ZstdDecompressor()


class BackupError(RuntimeError):
    """The configured backup is missing, ambiguous, or unreadable."""


@dataclass(frozen=True)
class Contact:
    username: str
    nickname: str = ""
    remark: str = ""
    alias: str = ""

    @property
    def display_name(self) -> str:
        return self.remark or self.nickname or self.alias or self.username


@dataclass(frozen=True)
class MessageTable:
    db_path: Path
    table: str
    username: str = ""


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        yield conn
    except sqlite3.DatabaseError as exc:
        raise BackupError(
            f"无法只读打开 {path}；请确认它是已解密的 SQLite 备份，而不是微信原始加密库"
        ) from exc
    finally:
        if conn is not None:
            conn.close()


def _quote_identifier(name: str) -> str:
    if not _SAFE_IDENTIFIER_RE.fullmatch(name):
        raise BackupError(f"数据库包含不安全的标识符: {name!r}")
    return f"[{name}]"


def _find_table(conn: sqlite3.Connection, candidates: tuple[str, ...]) -> str | None:
    tables = {
        str(row[0]).casefold(): str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
        )
    }
    return next(
        (tables[name.casefold()] for name in candidates if name.casefold() in tables),
        None,
    )


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        str(row[1]).casefold(): str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    }


def _find_column(columns: dict[str, str], *candidates: str) -> str | None:
    return next(
        (columns[name.casefold()] for name in candidates if name.casefold() in columns),
        None,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _decode_content(raw: Any, compression: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        data = bytes(raw)
        try:
            compressed = int(compression or 0) == 4
        except (TypeError, ValueError):
            compressed = False
        if compressed:
            try:
                data = _ZSTD.decompress(data)
            except zstandard.ZstdError:
                return "[无法解压的消息]"
        return data.decode("utf-8", "replace")
    return str(raw)


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise ValueError(f"limit 必须在 1 到 {_MAX_LIMIT} 之间")
    return limit


def _validate_query(value: str, name: str, *, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError(f"{name} 不能为空")
    if len(value) > _MAX_QUERY_LENGTH:
        raise ValueError(f"{name} 不能超过 {_MAX_QUERY_LENGTH} 个字符")
    return value


def _conversation_id(username: str) -> str:
    digest = hashlib.md5(username.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"Msg_{digest}"


def _normal_timestamp(value: Any) -> int:
    timestamp = int(value or 0)
    return timestamp // 1_000 if timestamp > 10_000_000_000 else timestamp


def _readable_time(timestamp: int) -> str | None:
    if timestamp <= 0:
        return None
    try:
        return (
            datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")
        )
    except (OSError, OverflowError, ValueError):
        return None


def _base_message_type(value: Any) -> int:
    try:
        return int(value) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def _truncate(value: str, length: int = _MAX_CONTENT_LENGTH) -> str:
    value = value.replace("\x00", "").strip()
    return value if len(value) <= length else f"{value[:length]}…"


def _xml_summary(value: str) -> str:
    value = value[:50_000]
    parts: list[str] = []
    for tag in _XML_TAGS:
        match = re.search(
            rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>",
            value,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            text = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
            if text and text not in parts:
                parts.append(text)
    return " | ".join(parts)


def _format_content(value: str, message_type: Any) -> str:
    base_type = _base_message_type(message_type)
    label = _MESSAGE_TYPES.get(base_type, f"类型 {base_type}")
    value = value.strip()
    if base_type in {1, 10000, 10002}:
        return _truncate(value) or f"[{label}]"
    if base_type == 49 and value.lstrip().startswith("<"):
        summary = _xml_summary(value)
        return f"[{label}] {summary}".strip()
    if value and not value.lstrip().startswith("<"):
        return _truncate(f"[{label}] {value}")
    return f"[{label}]"


class WeChatBackup:
    """Read a single account's plaintext WeChat 4.x SQLite backup."""

    def __init__(self, root: str | Path, *, sanitize: bool = False):
        self.root = Path(root).expanduser().resolve()
        self.sanitize = sanitize
        if not self.root.is_dir():
            raise BackupError(f"备份目录不存在或不是目录: {self.root}")

        self.message_dbs = self._discover_message_dbs()
        self.contact_db = self._discover_contact_db()

    @classmethod
    def from_environment(cls) -> WeChatBackup:
        root = os.environ.get("WECHAT_BACKUP_DIR") or os.environ.get("WECHAT_PLAIN_DIR")
        if not root:
            raise BackupError("请设置 WECHAT_BACKUP_DIR（或兼容变量 WECHAT_PLAIN_DIR）")
        sanitize = os.environ.get("SANITIZE", "0").strip().casefold() in _TRUE_VALUES
        return cls(root, sanitize=sanitize)

    def _discover_message_dbs(self) -> tuple[Path, ...]:
        files = tuple(
            path.resolve()
            for path in self.root.rglob("message_*.db")
            if path.is_file() and _MSG_DB_RE.fullmatch(path.name)
        )
        if not files:
            raise BackupError(
                f"{self.root} 下未找到 message_0.db / message_N.db；请指向单个账号的明文备份根目录"
            )
        parents = {path.parent for path in files}
        if len(parents) != 1:
            locations = ", ".join(sorted(str(path) for path in parents))
            raise BackupError(
                f"找到多组消息备份（{locations}）；请把目录缩小到单个账号、单个快照"
            )
        return tuple(
            sorted(
                files,
                key=lambda path: (
                    int(_MSG_DB_RE.fullmatch(path.name).group(1)),
                    str(path),
                ),
            )
        )

    def _discover_contact_db(self) -> Path | None:
        files = tuple(
            path.resolve()
            for path in self.root.rglob("*.db")
            if path.is_file() and path.name.casefold() == "contact.db"
        )
        if len(files) > 1:
            locations = ", ".join(sorted(str(path) for path in files))
            raise BackupError(
                f"找到多个 contact.db（{locations}）；请把目录缩小到单个账号、单个快照"
            )
        return files[0] if files else None

    @cached_property
    def contacts(self) -> tuple[Contact, ...]:
        if self.contact_db is None:
            return ()
        with _connect(self.contact_db) as conn:
            table = _find_table(conn, ("contact", "rcontact", "friend"))
            if table is None:
                raise BackupError(f"{self.contact_db} 中未找到联系人表")
            columns = _columns(conn, table)
            username = _find_column(columns, "username", "user_name", "wxid")
            if username is None:
                raise BackupError(f"{self.contact_db} 的联系人表缺少 username 列")

            def expression(candidates: tuple[str, ...], alias: str) -> str:
                column = _find_column(columns, *candidates)
                return (
                    f"{_quote_identifier(column)} AS {alias}"
                    if column
                    else f"'' AS {alias}"
                )

            sql = (
                f"SELECT {_quote_identifier(username)} AS username, "
                f"{expression(('nick_name', 'nickname', 'nickName'), 'nickname')}, "
                f"{expression(('remark', 'remark_name', 'conRemark'), 'remark')}, "
                f"{expression(('alias',), 'alias')} "
                f"FROM {_quote_identifier(table)}"
            )
            found: dict[str, Contact] = {}
            for row in conn.execute(sql):
                username_text = _as_text(row["username"]).strip()
                if username_text:
                    found[username_text] = Contact(
                        username=username_text,
                        nickname=_as_text(row["nickname"]).strip(),
                        remark=_as_text(row["remark"]).strip(),
                        alias=_as_text(row["alias"]).strip(),
                    )
            return tuple(found.values())

    @cached_property
    def contact_names(self) -> dict[str, str]:
        return {contact.username: contact.display_name for contact in self.contacts}

    @staticmethod
    def _name2id(conn: sqlite3.Connection) -> dict[int, str]:
        table = _find_table(conn, ("Name2Id",))
        if table is None:
            return {}
        columns = _columns(conn, table)
        username = _find_column(columns, "user_name", "username")
        if username is None:
            return {}
        return {
            int(row["internal_id"]): _as_text(row["username"]).strip()
            for row in conn.execute(
                f"SELECT rowid AS internal_id, {_quote_identifier(username)} AS username "
                f"FROM {_quote_identifier(table)}"
            )
            if _as_text(row["username"]).strip()
        }

    @staticmethod
    def _message_table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = ? AND name LIKE ? ORDER BY name",
                ("table", "Msg_%"),
            )
            if _MSG_TABLE_RE.fullmatch(str(row[0]))
        )

    @cached_property
    def message_tables(self) -> tuple[MessageTable, ...]:
        result: list[MessageTable] = []
        for db_path in self.message_dbs:
            with _connect(db_path) as conn:
                usernames = self._name2id(conn).values()
                table_to_username = {
                    _conversation_id(username).casefold(): username
                    for username in usernames
                }
                result.extend(
                    MessageTable(
                        db_path, table, table_to_username.get(table.casefold(), "")
                    )
                    for table in self._message_table_names(conn)
                )
        return tuple(result)

    @staticmethod
    def _message_expressions(conn: sqlite3.Connection, table: str) -> dict[str, str]:
        if not _MSG_TABLE_RE.fullmatch(table):
            raise BackupError(f"不安全的消息表名: {table!r}")
        columns = _columns(conn, table)

        def required(*candidates: str) -> str:
            column = _find_column(columns, *candidates)
            if column is None:
                raise BackupError(f"消息表 {table} 缺少列: {candidates[0]}")
            return _quote_identifier(column)

        def optional(default: str, *candidates: str) -> str:
            column = _find_column(columns, *candidates)
            return _quote_identifier(column) if column else default

        return {
            "local_id": optional("rowid", "local_id"),
            "local_type": required("local_type", "type"),
            "create_time": required("create_time", "createTime"),
            "real_sender_id": optional("0", "real_sender_id"),
            "content": required("message_content", "content"),
            "compression": optional("0", "WCDB_CT_message_content"),
        }

    @staticmethod
    def _select_list(expressions: dict[str, str]) -> str:
        return ", ".join(
            f"{expression} AS {name}" for name, expression in expressions.items()
        )

    def _display_name(self, username: str, fallback: str = "未知会话") -> str:
        return self.contact_names.get(username, username or fallback)

    def _public_username(self, username: str) -> str:
        if not username or not self.sanitize:
            return username
        suffix = "@chatroom" if username.endswith("@chatroom") else ""
        base = username.removesuffix(suffix)
        masked = "***" if len(base) < 8 else f"{base[:4]}***{base[-3:]}"
        return f"{masked}{suffix}"

    @staticmethod
    def _kind(username: str) -> str:
        if username.endswith("@chatroom"):
            return "group"
        if username.startswith("gh_"):
            return "official_account"
        return "private"

    def _resolve_conversation(self, value: str) -> tuple[MessageTable, ...]:
        value = _validate_query(value, "conversation")
        direct = tuple(
            table
            for table in self.message_tables
            if table.table.casefold() == value.casefold()
        )
        if direct:
            return direct

        exact: set[str] = set()
        partial: set[str] = set()
        needle = value.casefold()
        usernames = {table.username for table in self.message_tables if table.username}
        for username in usernames:
            fields = (username, self._display_name(username))
            if any(field.casefold() == needle for field in fields):
                exact.add(username)
            elif any(needle in field.casefold() for field in fields):
                partial.add(username)
        for contact in self.contacts:
            fields = (contact.username, contact.nickname, contact.remark, contact.alias)
            if any(field and field.casefold() == needle for field in fields):
                exact.add(contact.username)
            elif any(field and needle in field.casefold() for field in fields):
                partial.add(contact.username)

        matches = exact or partial
        if not matches:
            raise BackupError(f"未找到会话: {value}")
        if len(matches) > 1:
            names = ", ".join(
                sorted(self._display_name(username) for username in matches)[:10]
            )
            raise BackupError(
                f"“{value}”匹配到多个会话（{names}）；请先用 list_contacts 获取 conversation"
            )
        username = next(iter(matches))
        token = _conversation_id(username)
        resolved = tuple(
            table
            for table in self.message_tables
            if table.table.casefold() == token.casefold()
        )
        if not resolved:
            raise BackupError(f"{self._display_name(username)} 没有可读取的消息表")
        return resolved

    @staticmethod
    def _split_group_sender(content: str, is_group: bool) -> tuple[str, str]:
        if not is_group or ":\n" not in content:
            return "", content
        sender, text = content.split(":\n", 1)
        return (sender, text) if _GROUP_SENDER_RE.fullmatch(sender) else ("", content)

    def _message_record(
        self,
        context: MessageTable,
        row: sqlite3.Row,
        id_to_username: dict[int, str],
    ) -> dict[str, Any]:
        username = context.username
        is_group = username.endswith("@chatroom")
        decoded = _decode_content(row["content"], row["compression"])
        sender_in_content, content = self._split_group_sender(decoded, is_group)
        try:
            mapped_sender = id_to_username.get(int(row["real_sender_id"] or 0), "")
        except (TypeError, ValueError):
            mapped_sender = ""

        if is_group:
            sender_username = sender_in_content or (
                mapped_sender if mapped_sender != username else ""
            )
            is_sender = not sender_username
        else:
            sender_username = username if mapped_sender == username else ""
            is_sender = not sender_username

        timestamp = _normal_timestamp(row["create_time"])
        record: dict[str, Any] = {
            "conversation": context.table,
            "conversation_name": self._display_name(username, context.table),
            "conversation_username": self._public_username(username),
            "time": _readable_time(timestamp),
            "timestamp": timestamp,
            "sender": "我" if is_sender else self._display_name(sender_username),
            "is_sender": is_sender,
            "type": _MESSAGE_TYPES.get(
                _base_message_type(row["local_type"]),
                f"类型 {_base_message_type(row['local_type'])}",
            ),
            "content": _format_content(content, row["local_type"]),
        }
        if sender_username:
            record["sender_username"] = self._public_username(sender_username)
        return record

    def list_contacts(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """List or search contacts in the backup."""
        query = _validate_query(query, "query", allow_empty=True).casefold()
        limit = _validate_limit(limit)
        if self.contact_db is None:
            raise BackupError(f"{self.root} 下未找到 contact.db")
        contacts = [
            contact
            for contact in self.contacts
            if not query
            or any(
                query in field.casefold()
                for field in (
                    contact.username,
                    contact.nickname,
                    contact.remark,
                    contact.alias,
                )
                if field
            )
        ]
        contacts.sort(key=lambda contact: contact.display_name.casefold())
        return [
            {
                "conversation": _conversation_id(contact.username),
                "name": contact.display_name,
                "username": self._public_username(contact.username),
                "nickname": contact.nickname,
                "remark": contact.remark,
                "kind": self._kind(contact.username),
            }
            for contact in contacts[:limit]
        ]

    def list_conversations(self, limit: int = 30) -> list[dict[str, Any]]:
        """List conversations ordered by their latest message."""
        limit = _validate_limit(limit)
        aggregated: dict[str, dict[str, Any]] = {}
        for db_path, grouped in groupby(
            self.message_tables, key=lambda table: table.db_path
        ):
            with _connect(db_path) as conn:
                for context in grouped:
                    expressions = self._message_expressions(conn, context.table)
                    row = conn.execute(
                        f"SELECT COUNT(*) AS message_count, "
                        f"MAX({expressions['create_time']}) AS last_time "
                        f"FROM {_quote_identifier(context.table)}"
                    ).fetchone()
                    item = aggregated.setdefault(
                        context.table,
                        {
                            "conversation": context.table,
                            "name": self._display_name(context.username, context.table),
                            "username": self._public_username(context.username),
                            "kind": self._kind(context.username),
                            "message_count": 0,
                            "last_timestamp": 0,
                        },
                    )
                    item["message_count"] += int(row["message_count"] or 0)
                    item["last_timestamp"] = max(
                        item["last_timestamp"], _normal_timestamp(row["last_time"])
                    )
        result = sorted(
            aggregated.values(),
            key=lambda item: (item["last_timestamp"], item["name"]),
            reverse=True,
        )[:limit]
        for item in result:
            item["last_message_time"] = _readable_time(item.pop("last_timestamp"))
        return result

    def read_chat_history(
        self, conversation: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Read the latest messages from one conversation, oldest first."""
        limit = _validate_limit(limit)
        contexts = self._resolve_conversation(conversation)
        records: list[dict[str, Any]] = []
        for context in contexts:
            with _connect(context.db_path) as conn:
                expressions = self._message_expressions(conn, context.table)
                rows = conn.execute(
                    f"SELECT {self._select_list(expressions)} "
                    f"FROM {_quote_identifier(context.table)} "
                    f"ORDER BY {expressions['create_time']} DESC, {expressions['local_id']} DESC "
                    f"LIMIT ?",
                    (limit,),
                ).fetchall()
                id_to_username = self._name2id(conn)
                records.extend(
                    self._message_record(context, row, id_to_username) for row in rows
                )
        records.sort(key=lambda row: row["timestamp"], reverse=True)
        return list(reversed(records[:limit]))

    def search_messages(self, keyword: str, limit: int = 30) -> list[dict[str, Any]]:
        """Search all message tables for a case-insensitive substring, newest first."""
        keyword = _validate_query(keyword, "keyword")
        limit = _validate_limit(limit)
        needle = keyword.casefold()
        records: list[dict[str, Any]] = []

        # ponytail: this deliberately full-scans the immutable backup; add an external FTS index only when measured slow.
        for db_path, grouped in groupby(
            self.message_tables, key=lambda table: table.db_path
        ):
            with _connect(db_path) as conn:
                conn.create_function(
                    "_wechat_contains",
                    2,
                    lambda raw, compression: int(
                        needle in _decode_content(raw, compression).casefold()
                    ),
                )
                id_to_username = self._name2id(conn)
                for context in grouped:
                    expressions = self._message_expressions(conn, context.table)
                    rows = conn.execute(
                        f"SELECT {self._select_list(expressions)} "
                        f"FROM {_quote_identifier(context.table)} "
                        f"WHERE _wechat_contains({expressions['content']}, {expressions['compression']}) = 1 "
                        f"ORDER BY {expressions['create_time']} DESC, {expressions['local_id']} DESC "
                        f"LIMIT ?",
                        (limit,),
                    ).fetchall()
                    records.extend(
                        self._message_record(context, row, id_to_username)
                        for row in rows
                    )
        records.sort(key=lambda row: row["timestamp"], reverse=True)
        return records[:limit]


mcp = FastMCP(
    "wechat-backup",
    instructions=(
        "只读查询用户本人授权的本地微信明文备份。"
        "不得发送消息、修改数据库、索取密钥、执行任意 SQL 或导出完整数据库。"
    ),
)


@lru_cache(maxsize=1)
def _backup() -> WeChatBackup:
    return WeChatBackup.from_environment()


@mcp.tool()
def list_conversations(limit: int = 30) -> list[dict[str, Any]]:
    """列出最近微信会话，包含可供后续查询使用的 conversation、显示名、消息数和最后消息时间。"""
    return _backup().list_conversations(limit)


@mcp.tool()
def list_contacts(query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """列出或按昵称、备注、微信标识搜索联系人；query 留空时列出联系人。"""
    return _backup().list_contacts(query, limit)


@mcp.tool()
def read_chat_history(conversation: str, limit: int = 50) -> list[dict[str, Any]]:
    """读取一个会话最近的消息，按时间从旧到新返回；conversation 优先使用列表工具返回的值。"""
    return _backup().read_chat_history(conversation, limit)


@mcp.tool()
def search_messages(keyword: str, limit: int = 30) -> list[dict[str, Any]]:
    """在全部备份消息中按关键字做不区分大小写的子串搜索，按时间从新到旧返回。"""
    return _backup().search_messages(keyword, limit)


@mcp.tool()
def analyze_message(
    message: str, relation: str = "", severity_hint: str = "", context: str = ""
) -> dict[str, Any]:
    """判断一条消息的意图（表达感情 / 想要解决答案 / 纯聊天）、发送者情绪（快乐、悲伤、愤怒、恐惧、惊讶、厌恶）、
    细分状态（开心/难过/生气/委屈/焦虑/失望/尴尬/疲惫）、真实诉求（倾诉/安慰/解决/道歉/空间/被重视）和事情严重程度，
    并给出不让对方难受的回复策略。relation 可填 朋友/恋人/同事/领导/客户/不熟；severity_hint 可填 小事/认真/严重；
    context 可放最近几条对话（每行一条，如“我：…”“对方：…”）。
    配置了 TYPESAFE_API_KEY 时由 TypeSafe Jev 模型判断（source=jev，附带置信度和概率），否则退回离线关键词规则（source=rules）。
    结果只是第一层参考，最终措辞请结合上下文调整。"""
    return jev.analyze(message, relation, severity_hint, context)


@mcp.prompt()
def empathetic_reply(message: str, relation: str = "", context: str = "") -> str:
    """按“情绪 + 诉求 + 关系 + 严重程度 + 语气”的顺序，为一条消息拟一个不让对方难受的回复。"""
    judged = jev.analyze(message, relation, context=context)
    strategy = judged["reply_strategy"]
    source = "Jev 模型" if judged.get("source") == "jev" else "离线规则"
    context_lines = (
        ["最近上下文：", context.strip(), ""] if context.strip() else []
    )
    lines = [
        "请为下面这条消息拟一个回复。",
        "",
        f"对方发来：{message}",
        f"我们的关系：{judged['relation_label']}",
        "",
        *context_lines,
        f"{source}判断（仅供参考，可结合上下文修正）：",
        f"- 情绪：{judged['emotion']}（细分：{judged['state'] or '不明显'}）",
        f"- 意图：{judged['intent_label']}",
        f"- 诉求：{judged['need_label']}",
        f"- 严重程度：{judged['severity_label']}",
        f"- 依据：{'；'.join(judged['signals']) or '无明显线索'}",
        "",
        f"回复顺序：{' → '.join(strategy['order'])}",
        f"语气：{strategy['tone']}",
        f"要做：{strategy['do']}",
        f"避免：{'；'.join(strategy['avoid'])}",
        f"可参考的开头：{strategy['opening_template']}",
        "",
        "判断准则：",
        empathy.GUIDELINE,
        "输出要求：先用一两句话说明你对情绪和诉求的判断，再给出 1 条可以直接发送的回复"
        "（口语、微信风格、不超过 4 句），必要时再给 1 条更简短的备选。不要说出准则里列出的伤人话。",
    ]
    return "\n".join(line for line in lines if line is not None)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
