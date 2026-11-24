"""SQLite persistence for FileBot's index, event queue, and command audit."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal


DirtyOperation = Literal["upsert", "delete"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    ext TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0 CHECK(size_bytes >= 0),
    mtime REAL NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK(chunk_count >= 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'indexed', 'failed', 'skipped_large')),
    last_indexed REAL,
    error TEXT,
    indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(file_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_files_modified ON files(mtime);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    page_or_line TEXT,
    heading TEXT,
    snippet TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0 CHECK(token_count >= 0),
    vector_id TEXT,
    UNIQUE(file_id, chunk_index),
    CHECK(length(chunk_id) > 0)
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE TABLE IF NOT EXISTS dirty_queue (
    path TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK(event_type IN ('upsert', 'delete')),
    queued_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_dirty_queue_time ON dirty_queue(queued_at);

CREATE TABLE IF NOT EXISTS file_signals (
    file_id INTEGER PRIMARY KEY REFERENCES files(file_id) ON DELETE CASCADE,
    open_count INTEGER NOT NULL DEFAULT 0 CHECK(open_count >= 0),
    upvotes INTEGER NOT NULL DEFAULT 0 CHECK(upvotes >= 0),
    downvotes INTEGER NOT NULL DEFAULT 0 CHECK(downvotes >= 0),
    last_opened REAL
);

CREATE TABLE IF NOT EXISTS glossary (
    alias TEXT PRIMARY KEY COLLATE NOCASE,
    expansion TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cmd_audit (
    proposal_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL,
    cwd TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL CHECK(risk_level IN ('read_only', 'gated', 'blocked')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected', 'blocked', 'executed', 'failed')),
    created_at REAL NOT NULL,
    approved_at REAL,
    executed_at REAL,
    exit_code INTEGER,
    stdout_tail TEXT,
    stderr_tail TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmd_audit_status ON cmd_audit(status, created_at);
"""

class Database:
    """Small connection-per-operation SQLite wrapper using WAL mode.

    A fresh connection per operation is safe for the app's UI and worker
    threads. The database file's parent directory is created automatically.
    """

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path).expanduser()
        self.timeout = timeout
        if timeout <= 0:
            raise ValueError("timeout must be positive")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection and reliably close it afterward.

        SQLite exceptions propagate to the caller so the UI/worker can report
        failures without silently losing index or audit data.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the schema and enable persistent write-ahead logging."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not initialize FileBot database at {self.path}: {exc}") from exc

    def enqueue_dirty(self, path: str | Path, operation: DirtyOperation) -> None:
        """Insert or replace a debounced filesystem change in ``dirty_queue``."""
        if operation not in ("upsert", "delete"):
            raise ValueError(f"Unsupported dirty operation: {operation!r}")
        normalized = str(Path(path).expanduser().resolve())
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO dirty_queue(path, event_type, queued_at, attempts, last_error)
                       VALUES (?, ?, ?, 0, NULL)
                       ON CONFLICT(path) DO UPDATE SET event_type=excluded.event_type,
                           queued_at=excluded.queued_at, attempts=0, last_error=NULL""",
                    (normalized, operation, datetime.now(timezone.utc).timestamp()),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not queue {operation} for {normalized}: {exc}") from exc

    def pending_dirty(self, limit: int = 100) -> list[sqlite3.Row]:
        """Return oldest queued filesystem changes without removing them."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        try:
            with self.connect() as connection:
                return list(connection.execute(
                    "SELECT path, event_type AS operation, queued_at, attempts, last_error "
                    "FROM dirty_queue ORDER BY queued_at LIMIT ?", (limit,)
                ))
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not read dirty queue: {exc}") from exc

    def remove_dirty(self, path: str | Path) -> None:
        """Remove a successfully processed path from ``dirty_queue``."""
        normalized = str(Path(path).expanduser().resolve())
        try:
            with self.connect() as connection:
                connection.execute("DELETE FROM dirty_queue WHERE path = ?", (normalized,))
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not remove queued path {normalized}: {exc}") from exc

    def record_dirty_error(self, path: str | Path, error: str) -> None:
        """Record a processing failure while retaining the item for retry."""
        normalized = str(Path(path).expanduser().resolve())
        try:
            with self.connect() as connection:
                connection.execute(
                    "UPDATE dirty_queue SET attempts = attempts + 1, last_error = ? WHERE path = ?",
                    (error[:2000], normalized),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not record queue error for {normalized}: {exc}") from exc

