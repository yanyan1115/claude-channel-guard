#!/usr/bin/env python3
"""Private state and policy checks for Telegram and memory-write guards."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_STATE_DIR = Path(os.environ.get("CHANNEL_GUARD_DIR", Path.home() / ".claude" / "channel-guard"))
DEFAULT_BURST_SECONDS = int(os.environ.get("CHANNEL_GUARD_BURST_SECONDS", "30"))
DEFAULT_MAX_OUTBOUND = int(os.environ.get("CHANNEL_GUARD_MAX_OUTBOUND", "8"))
DEFAULT_MAX_CHARS = int(os.environ.get("CHANNEL_GUARD_MAX_CHARS", "6000"))
DEFAULT_GRANT_START_TTL = int(os.environ.get("CHANNEL_GUARD_GRANT_START_TTL", "600"))

MEMORY_WRITE_TOOLS = {
    "memory_remember",
    "memory_save",
    "memory_update",
    "memory_delete",
    "memory_forget",
    "memory_reindex",
    "reindex_embeddings",
    "experience_append",
}

HIGH_IMPACT_MEMORY_TOOLS = {
    "memory_update",
    "memory_delete",
    "memory_forget",
    "memory_reindex",
    "reindex_embeddings",
    "experience_append",
}

MEMORY_INTENT_PATTERNS = [
    r"记(?:一下|住|下来|到记忆|进记忆|在记忆里)",
    r"帮我(?:存|记|保存|留)(?:一下|下来|到下次)?",
    r"以后按这个(?:来|做)",
    r"下次(?:记得|要记住)",
    r"这个(?:要|帮我)留到下次",
    r"更新(?:一下)?记忆",
    r"删掉(?:这条|这个|记忆)",
    r"忘记(?:这条|这个|刚才)",
    r"别再记(?:这个|这条)",
    r"\bremember this\b",
    r"\bsave this\b",
    r"\bforget this\b",
    r"\bupdate memory\b",
]

DELETE_INTENT_PATTERNS = [
    r"删掉(?:这条|这个|记忆)",
    r"忘记(?:这条|这个|刚才)",
    r"别再记(?:这个|这条)",
    r"\bforget this\b",
    r"\bdelete (?:this )?memory\b",
]

MEMORY_INTENT_PHRASES = [
    "记一下",
    "记住",
    "记下来",
    "帮我存",
    "帮我记",
    "帮我保存",
    "以后按这个来",
    "以后按这个做",
    "下次记得",
    "留到下次",
    "更新记忆",
    "删掉这条",
    "忘记这个",
    "别再记",
]

DELETE_INTENT_PHRASES = [
    "删掉这条",
    "删掉这个",
    "忘记这个",
    "忘记这条",
    "别再记",
]

RELATIONSHIP_RISK_PATTERNS = [
    r"亲(?:了|吻|密)",
    r"拥抱",
    r"恋人",
    r"伴侣",
    r"承诺",
    r"同意了",
    r"关系(?:变|升级|确认)",
    r"边界(?:变化|改变|更新)",
    r"身体",
    r"\bkiss",
    r"\bhug",
    r"\bpartner\b",
    r"\bpromise\b",
    r"\bconsent",
    r"\bboundar",
]


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except PermissionError:
        pass


def _read_or_create_salt(state_dir: Path) -> bytes:
    ensure_private_dir(state_dir)
    salt_path = state_dir / "salt"
    if salt_path.exists():
        return salt_path.read_bytes().strip()
    salt = uuid.uuid4().hex.encode("ascii")
    salt_path.write_bytes(salt)
    try:
        os.chmod(salt_path, 0o600)
    except PermissionError:
        pass
    return salt


def _chmod_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except PermissionError:
        pass


def _normal_text(value: Any) -> str:
    return str(value or "").strip()


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    words = re.findall(r"[a-z0-9_+#.-]{2,}", lowered)
    cjk = re.findall(r"[\u3400-\u9fff]", lowered)
    grams = {"".join(cjk[i : i + 2]) for i in range(max(0, len(cjk) - 1))}
    grams.update("".join(cjk[i : i + 3]) for i in range(max(0, len(cjk) - 2)))
    return {t for t in words + list(grams) if t.strip()}


def _strip_memory_intent(text: str) -> str:
    cleaned = text
    for phrase in MEMORY_INTENT_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    for pattern in MEMORY_INTENT_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[:：,，。.!！\s]+", " ", cleaned)
    return cleaned.strip()


@dataclass
class GuardDecision:
    allow: bool
    reason: str
    grant_id: str | None = None
    review: bool = False


class ChannelGuardState:
    def __init__(self, state_dir: Path | str | None = None) -> None:
        self.state_dir = Path(state_dir or DEFAULT_STATE_DIR)
        ensure_private_dir(self.state_dir)
        self.salt = _read_or_create_salt(self.state_dir)
        self.db_path = self.state_dir / "guard_state.sqlite"
        self.ledger_path = self.state_dir / "inbound-ledger.jsonl"
        self.guard_log = self.state_dir / "guard.log"
        self.blocked_log = self.state_dir / "blocked.log"
        self.pending_log = self.state_dir / "memory-pending.log"
        self._init_db()

    def hash_value(self, value: Any) -> str:
        h = hashlib.sha256()
        h.update(self.salt)
        h.update(_normal_text(value).encode("utf-8", "replace"))
        return h.hexdigest()[:24]

    def content_hash(self, text: str) -> str:
        return hashlib.sha256(_normal_text(text).encode("utf-8", "replace")).hexdigest()[:24]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    grant_id TEXT NOT NULL UNIQUE,
                    chat_hash TEXT NOT NULL,
                    user_hash TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    token_hashes TEXT NOT NULL DEFAULT '[]',
                    memory_intent INTEGER NOT NULL DEFAULT 0,
                    delete_intent INTEGER NOT NULL DEFAULT 0,
                    risk_flags TEXT NOT NULL DEFAULT '[]',
                    telegram_ts TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(chat_hash, message_id)
                );
                CREATE TABLE IF NOT EXISTS grants (
                    grant_id TEXT PRIMARY KEY,
                    chat_hash TEXT NOT NULL,
                    user_hash TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    first_outbound_at REAL,
                    last_outbound_at REAL,
                    outbound_count INTEGER NOT NULL DEFAULT 0,
                    outbound_chars INTEGER NOT NULL DEFAULT 0,
                    closed_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_grants_chat_status ON grants(chat_hash, status, created_at);
                CREATE TABLE IF NOT EXISTS guard_health (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            try:
                os.chmod(self.db_path, 0o600)
            except PermissionError:
                pass
            db.commit()

    def log(self, decision: str, tool_name: str, reason: str, **fields: Any) -> None:
        record = {
            "time": iso_now(),
            "decision": decision,
            "tool_name": tool_name,
            "reason": reason,
            **{k: v for k, v in fields.items() if v is not None},
        }
        path = self.blocked_log if decision == "deny" else self.guard_log
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        _chmod_private_file(path)

    def set_health(self, key: str, value: str) -> None:
        with closing(self._connect()) as db:
            db.execute(
                """
                INSERT INTO guard_health(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now_ts()),
            )
            db.commit()

    def get_health(self, key: str, default: str = "") -> str:
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM guard_health WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def record_inbound(self, content: str, meta: dict[str, Any]) -> str | None:
        chat_id = _normal_text(meta.get("chat_id"))
        user_id = _normal_text(meta.get("user_id") or meta.get("user"))
        message_id = _normal_text(meta.get("message_id"))
        if not chat_id or not user_id or not message_id:
            self.log("deny", "inbound", "missing_inbound_identity")
            return None

        content = _normal_text(content)
        grant_id = uuid.uuid4().hex
        chat_hash = self.hash_value(chat_id)
        user_hash = self.hash_value(user_id)
        token_hashes = sorted(self.hash_value(token) for token in _tokens(_strip_memory_intent(content)))
        memory_intent = any(re.search(p, content, re.IGNORECASE) for p in MEMORY_INTENT_PATTERNS)
        delete_intent = any(re.search(p, content, re.IGNORECASE) for p in DELETE_INTENT_PATTERNS)
        risks = self.risk_flags(content)
        created = now_ts()
        lowered = content.lower()
        memory_intent = memory_intent or any(phrase.lower() in lowered for phrase in MEMORY_INTENT_PHRASES)
        delete_intent = delete_intent or any(phrase.lower() in lowered for phrase in DELETE_INTENT_PHRASES)

        with closing(self._connect()) as db:
            existing = db.execute(
                "SELECT grant_id FROM inbound_messages WHERE chat_hash = ? AND message_id = ?",
                (chat_hash, message_id),
            ).fetchone()
            if existing:
                self.log(
                    "deny",
                    "inbound",
                    "duplicate_inbound_message",
                    grant_id=str(existing["grant_id"]),
                    chat_hash=chat_hash,
                )
                return None

            cur = db.execute(
                """
                INSERT INTO inbound_messages(
                    grant_id, chat_hash, user_hash, message_id, message_hash,
                    content_hash, token_hashes, memory_intent, delete_intent,
                    risk_flags, telegram_ts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    chat_hash,
                    user_hash,
                    message_id,
                    self.hash_value(message_id),
                    self.content_hash(content),
                    json.dumps(token_hashes),
                    1 if memory_intent else 0,
                    1 if delete_intent else 0,
                    json.dumps(risks, ensure_ascii=False),
                    _normal_text(meta.get("ts")),
                    created,
                ),
            )
            if cur.rowcount != 1:
                self.log("deny", "inbound", "duplicate_inbound_message", chat_hash=chat_hash)
                return None
            db.execute(
                "UPDATE grants SET status = 'closed', closed_reason = 'superseded_by_new_inbound' WHERE chat_hash = ? AND status = 'active'",
                (chat_hash,),
            )
            db.execute(
                """
                INSERT INTO grants(grant_id, chat_hash, user_hash, message_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (grant_id, chat_hash, user_hash, message_id, created),
            )
            db.commit()

        ledger = {
            "time": iso_now(),
            "grant_id": grant_id,
            "chat_hash": chat_hash,
            "user_hash": user_hash,
            "message_hash": self.hash_value(message_id),
            "content_hash": self.content_hash(content),
            "memory_intent": memory_intent,
            "delete_intent": delete_intent,
            "risk_flags": risks,
            "telegram_ts": _normal_text(meta.get("ts")),
        }
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n")
        _chmod_private_file(self.ledger_path)
        self.log("allow", "inbound", "recorded_inbound", grant_id=grant_id, chat_hash=chat_hash)
        return grant_id

    def risk_flags(self, text: str) -> list[str]:
        flags: list[str] = []
        if any(re.search(p, text, re.IGNORECASE) for p in RELATIONSHIP_RISK_PATTERNS):
            flags.append("relationship_or_boundary")
        return flags

    def _active_grant(self, chat_id: Any | None = None) -> sqlite3.Row | None:
        chat_hash = self.hash_value(chat_id) if chat_id not in (None, "") else None
        params: list[Any] = []
        where = "WHERE g.status = 'active'"
        if chat_hash:
            where += " AND g.chat_hash = ?"
            params.append(chat_hash)
        with closing(self._connect()) as db:
            row = db.execute(
                f"""
                SELECT g.*, i.memory_intent, i.delete_intent, i.token_hashes, i.risk_flags
                FROM grants g
                JOIN inbound_messages i ON i.grant_id = g.grant_id
                {where}
                ORDER BY g.created_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row

    def allow_telegram_outbound(self, tool_name: str, args: dict[str, Any]) -> GuardDecision:
        if self.get_health("upstream_status", "ok") != "ok":
            return GuardDecision(False, "upstream_hash_mismatch")
        chat_id = args.get("chat_id")
        row = self._active_grant(chat_id)
        if not row:
            return GuardDecision(False, "no_active_grant")
        now = now_ts()
        if not row["first_outbound_at"] and now - float(row["created_at"]) > DEFAULT_GRANT_START_TTL:
            self.close_grant(row["grant_id"], "expired_before_first_outbound")
            return GuardDecision(False, "expired_before_first_outbound", row["grant_id"])
        if row["first_outbound_at"] and now - float(row["first_outbound_at"]) > DEFAULT_BURST_SECONDS:
            self.close_grant(row["grant_id"], "burst_window_expired")
            return GuardDecision(False, "burst_window_expired", row["grant_id"])

        reply_to = args.get("reply_to") if tool_name == "reply" else args.get("message_id")
        if reply_to not in (None, "") and str(reply_to) != str(row["message_id"]):
            return GuardDecision(False, "message_id_mismatch", row["grant_id"])

        text = _normal_text(args.get("text"))
        files = args.get("files") if isinstance(args.get("files"), list) else []
        char_cost = len(text) + 256 * len(files)
        if int(row["outbound_count"]) + 1 > DEFAULT_MAX_OUTBOUND:
            self.close_grant(row["grant_id"], "outbound_count_exceeded")
            return GuardDecision(False, "outbound_count_exceeded", row["grant_id"])
        if int(row["outbound_chars"]) + char_cost > DEFAULT_MAX_CHARS:
            self.close_grant(row["grant_id"], "outbound_chars_exceeded")
            return GuardDecision(False, "outbound_chars_exceeded", row["grant_id"])

        with closing(self._connect()) as db:
            db.execute(
                """
                UPDATE grants
                SET first_outbound_at = COALESCE(first_outbound_at, ?),
                    last_outbound_at = ?,
                    outbound_count = outbound_count + 1,
                    outbound_chars = outbound_chars + ?
                WHERE grant_id = ?
                """,
                (now, now, char_cost, row["grant_id"]),
            )
            db.commit()
        return GuardDecision(True, "active_grant", row["grant_id"])

    def allow_memory_write(self, tool_name: str, args: dict[str, Any]) -> GuardDecision:
        if self.get_health("upstream_status", "ok") != "ok":
            return GuardDecision(False, "upstream_hash_mismatch", review=True)
        row = self._active_grant(args.get("chat_id"))
        if not row:
            return GuardDecision(False, "no_active_grant")
        if tool_name in HIGH_IMPACT_MEMORY_TOOLS:
            return GuardDecision(False, "high_impact_memory_write_requires_review", row["grant_id"], review=True)
        if not int(row["memory_intent"]):
            return GuardDecision(False, "no_explicit_memory_intent", row["grant_id"], review=True)

        content = _normal_text(args.get("content") or args.get("text") or args.get("value"))
        if not content:
            return GuardDecision(False, "missing_memory_content", row["grant_id"], review=True)
        if self.risk_flags(content):
            return GuardDecision(False, "risky_memory_content_requires_review", row["grant_id"], review=True)

        try:
            inbound_hashes = set(json.loads(row["token_hashes"] or "[]"))
        except json.JSONDecodeError:
            inbound_hashes = set()
        content_hashes = {self.hash_value(token) for token in _tokens(content)}
        if not content_hashes or not inbound_hashes:
            return GuardDecision(False, "insufficient_content_evidence", row["grant_id"], review=True)
        common = len(content_hashes & inbound_hashes)
        overlap = common / max(1, min(len(content_hashes), len(inbound_hashes)))
        if common < 2 and overlap < 0.30:
            return GuardDecision(False, "memory_content_not_grounded_in_inbound", row["grant_id"], review=True)
        return GuardDecision(True, "explicit_grounded_memory_intent", row["grant_id"])

    def close_grant(self, grant_id: str, reason: str) -> None:
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE grants SET status = 'closed', closed_reason = ? WHERE grant_id = ? AND status = 'active'",
                (reason, grant_id),
            )
            db.commit()

    def close_all(self, reason: str = "closed_by_hook") -> int:
        with closing(self._connect()) as db:
            cur = db.execute(
                "UPDATE grants SET status = 'closed', closed_reason = ? WHERE status = 'active'",
                (reason,),
            )
            db.commit()
            return int(cur.rowcount or 0)

    def close_consumed(self, reason: str = "closed_by_hook") -> int:
        with closing(self._connect()) as db:
            cur = db.execute(
                """
                UPDATE grants
                SET status = 'closed', closed_reason = ?
                WHERE status = 'active'
                  AND first_outbound_at IS NOT NULL
                """,
                (reason,),
            )
            db.commit()
            return int(cur.rowcount or 0)

    def append_pending(self, tool_name: str, args: dict[str, Any], reason: str, grant_id: str | None) -> None:
        record = {
            "time": iso_now(),
            "tool_name": tool_name,
            "reason": reason,
            "grant_id": grant_id,
            "content_hash": self.content_hash(json.dumps(args, ensure_ascii=False, sort_keys=True)),
        }
        with self.pending_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        _chmod_private_file(self.pending_log)


def tool_suffix(tool_name: str) -> str:
    if "__" in tool_name:
        return tool_name.rsplit("__", 1)[-1]
    return tool_name
