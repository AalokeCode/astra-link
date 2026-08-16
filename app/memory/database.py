"""SQLite storage (spec §18). Standard library only.

WAL mode so a read (e.g. the CLI listing recent tasks) never blocks the write
path. Schema changes go through `_MIGRATIONS`; `user_version` tracks position.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MIGRATIONS: list[str] = [
    # 1
    """
    CREATE TABLE conversations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL,
        source      TEXT NOT NULL,
        title       TEXT,
        summary     TEXT
    );

    CREATE TABLE messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        created_at      TEXT NOT NULL,
        role            TEXT NOT NULL,
        content         TEXT NOT NULL DEFAULT '',
        tool_calls      TEXT,
        tool_result     TEXT
    );
    CREATE INDEX idx_messages_conversation ON messages(conversation_id, id);

    CREATE TABLE tool_calls (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        created_at      TEXT NOT NULL,
        tool            TEXT NOT NULL,
        arguments       TEXT NOT NULL,
        outcome         TEXT NOT NULL,
        duration_ms     INTEGER NOT NULL DEFAULT 0,
        error           TEXT
    );
    CREATE INDEX idx_tool_calls_created ON tool_calls(created_at);

    CREATE TABLE preferences (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  TEXT NOT NULL,
        title       TEXT NOT NULL,
        due_at      TEXT,
        status      TEXT NOT NULL DEFAULT 'pending',
        external_id TEXT,
        notes       TEXT
    );

    CREATE TABLE projects (
        path        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        last_seen   TEXT NOT NULL,
        summary     TEXT,
        metadata    TEXT
    );
    """,
]


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock. Tool handlers run in worker
        # threads, and sqlite3 objects are not safe to share across them
        # without either this or a connection pool; a lock is simpler and the
        # write volume is trivial.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for version in range(current, len(_MIGRATIONS)):
                log.info("applying migration %d", version + 1)
                self._conn.executescript(_MIGRATIONS[version])
                self._conn.execute(f"PRAGMA user_version={version + 1}")
            self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- conversations ----------------------------------------------------

    def create_conversation(self, source: str, title: str | None = None) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (started_at, source, title) VALUES (?, ?, ?)",
                (_now(), source, title),
            )
            return int(cur.lastrowid)

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str = "",
        tool_calls: Any = None,
        tool_result: Any = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO messages
                   (conversation_id, created_at, role, content, tool_calls, tool_result)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id,
                    _now(),
                    role,
                    content,
                    json.dumps(tool_calls, default=str) if tool_calls else None,
                    json.dumps(tool_result, default=str) if tool_result else None,
                ),
            )

    def recent_messages(self, conversation_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            rows = cur.execute(
                """SELECT * FROM messages WHERE conversation_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (conversation_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def latest_conversation(self, source: str | None = None) -> sqlite3.Row | None:
        query = "SELECT * FROM conversations"
        params: tuple[Any, ...] = ()
        if source:
            query += " WHERE source = ?"
            params = (source,)
        query += " ORDER BY id DESC LIMIT 1"
        with self._cursor() as cur:
            return cur.execute(query, params).fetchone()

    def set_summary(self, conversation_id: int, summary: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE conversations SET summary = ? WHERE id = ?", (summary, conversation_id)
            )

    # -- tool call audit log ---------------------------------------------

    def log_tool_call(
        self,
        conversation_id: int | None,
        tool: str,
        arguments: dict[str, Any],
        outcome: str,
        duration_ms: int = 0,
        error: str | None = None,
    ) -> None:
        """Spec §30 requires every invocation be logged.

        Arguments are redacted before storage — a tool argument can carry a
        token or a password, and this table is long-lived.
        """
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO tool_calls
                   (conversation_id, created_at, tool, arguments, outcome, duration_ms, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id,
                    _now(),
                    tool,
                    json.dumps(redact(arguments), default=str),
                    outcome,
                    duration_ms,
                    error,
                ),
            )

    # -- preferences ------------------------------------------------------

    def set_preference(self, key: str, value: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                  updated_at=excluded.updated_at""",
                (key, value, _now()),
            )

    def get_preference(self, key: str) -> str | None:
        with self._cursor() as cur:
            row = cur.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def all_preferences(self) -> dict[str, str]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT key, value FROM preferences").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # -- projects ---------------------------------------------------------

    def remember_project(
        self, path: str, name: str, summary: str | None = None, metadata: Any = None
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO projects (path, name, last_seen, summary, metadata)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       name=excluded.name,
                       last_seen=excluded.last_seen,
                       summary=COALESCE(excluded.summary, projects.summary),
                       metadata=COALESCE(excluded.metadata, projects.metadata)""",
                (path, name, _now(), summary, json.dumps(metadata, default=str) if metadata else None),
            )

    # -- retention --------------------------------------------------------

    def prune(self, retention_days: int) -> dict[str, int]:
        """Bounded storage (spec §18, §33): drop what's past retention.

        Preferences and projects are exempt — those are the parts the user
        explicitly asked to be remembered.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        removed: dict[str, int] = {}
        with self._cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE started_at < ?", (cutoff,))
            removed["conversations"] = cur.rowcount
            cur.execute("DELETE FROM tool_calls WHERE created_at < ?", (cutoff,))
            removed["tool_calls"] = cur.rowcount
            cur.execute(
                "DELETE FROM tasks WHERE status = 'done' AND created_at < ?", (cutoff,)
            )
            removed["tasks"] = cur.rowcount
        if any(removed.values()):
            with self._lock:
                self._conn.execute("VACUUM")
        return removed

    def size_bytes(self) -> int:
        return self._path.stat().st_size if self._path.exists() else 0


_SECRET_HINTS = ("key", "token", "secret", "password", "passwd", "credential", "auth")


def redact(data: Any) -> Any:
    """Mask values whose key name suggests a secret (spec §30)."""
    if isinstance(data, dict):
        return {
            k: "***REDACTED***"
            if any(hint in k.lower() for hint in _SECRET_HINTS)
            else redact(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact(item) for item in data]
    if isinstance(data, str) and len(data) > 2000:
        return data[:2000] + f"...[{len(data) - 2000} more chars]"
    return data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
