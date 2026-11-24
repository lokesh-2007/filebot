"""SQLite persistence for FileBot's index, event queue, and command audit."""

from __future__ import annotations

import sqlite3
import hashlib
import uuid
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

    def get_file(self, path: str | Path) -> sqlite3.Row | None:
        """Return indexed file metadata for a normalized path."""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM files WHERE path=?", (str(Path(path).resolve()),)).fetchone()

    def replace_file(self, path: str | Path, *, content_hash: str, size: int, mtime: float,
                     chunks: list[dict[str, object]]) -> int:
        """Atomically replace file metadata and chunk records; return file id."""
        normalized = str(Path(path).resolve())
        with self.connect() as connection:
            connection.execute("""INSERT INTO files(path,file_name,ext,content_hash,size_bytes,mtime,chunk_count,status,last_indexed,error,indexed_at)
                VALUES(?,?,?,?,?,?,?,'indexed',strftime('%s','now'),NULL,datetime('now'))
                ON CONFLICT(path) DO UPDATE SET file_name=excluded.file_name,ext=excluded.ext,content_hash=excluded.content_hash,
                size_bytes=excluded.size_bytes,mtime=excluded.mtime,chunk_count=excluded.chunk_count,status='indexed',
                last_indexed=excluded.last_indexed,error=NULL,indexed_at=excluded.indexed_at""",
                (normalized, Path(normalized).name, Path(normalized).suffix.lower(), content_hash, size, mtime, len(chunks)))
            file_id = int(connection.execute("SELECT file_id FROM files WHERE path=?", (normalized,)).fetchone()[0])
            connection.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
            for index, item in enumerate(chunks):
                chunk_id = hashlib.sha256(f"{normalized}:{index}".encode("utf-8")).hexdigest()
                connection.execute("""INSERT INTO chunks(chunk_id,file_id,chunk_index,page_or_line,heading,snippet,token_count,vector_id)
                    VALUES(?,?,?,?,?,?,?,?)""", (chunk_id,file_id,index,item.get("page_or_line"),item.get("heading"),
                    str(item.get("text", "")),len(str(item.get("text", "")).split()),chunk_id))
            return file_id

    def delete_file(self, path: str | Path) -> None:
        """Remove an indexed file and its dependent chunks and signals."""
        with self.connect() as connection:
            connection.execute("DELETE FROM files WHERE path=?", (str(Path(path).resolve()),))

    def all_chunks(self) -> list[sqlite3.Row]:
        """Return chunk corpus records for the in-memory BM25 index."""
        with self.connect() as connection:
            return list(connection.execute("""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading
                FROM chunks c JOIN files f USING(file_id) WHERE f.status='indexed' ORDER BY f.file_id,c.chunk_index"""))

    def search_lexical(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """Fallback lexical search using SQLite LIKE when BM25 is not active."""
        terms = [f"%{term}%" for term in query.split() if term]
        if not terms:
            return []
        clause = " OR ".join("c.snippet LIKE ?" for _ in terms)
        with self.connect() as connection:
            return list(connection.execute(f"""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading
                FROM chunks c JOIN files f USING(file_id) WHERE {clause} LIMIT ?""", (*terms, limit)))

    def stage_command(self, command: str, cwd: str, reason: str, risk: str, status: str = "pending") -> str:
        """Create an immutable audit proposal and return its identifier."""
        proposal_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("""INSERT INTO cmd_audit(proposal_id,command,cwd,reason,risk_level,status,created_at)
                VALUES(?,?,?,?,?,?,strftime('%s','now'))""", (proposal_id,command,cwd,reason,risk,status))
        return proposal_id

    def pending_commands(self) -> list[sqlite3.Row]:
        """Return commands awaiting an explicit user decision."""
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM cmd_audit WHERE status='pending' ORDER BY created_at"))

    def decide_command(self, proposal_id: str, approved: bool) -> bool:
        """Record the explicit UI decision; return whether a pending row changed."""
        with self.connect() as connection:
            result = connection.execute("UPDATE cmd_audit SET status=?,approved_at=strftime('%s','now') WHERE proposal_id=? AND status='pending'",
                                        ("approved" if approved else "rejected", proposal_id))
            return result.rowcount == 1

    def command_record(self, proposal_id: str) -> sqlite3.Row | None:
        """Read one command audit record."""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM cmd_audit WHERE proposal_id=?", (proposal_id,)).fetchone()

    def claim_approved_command(self, proposal_id: str) -> sqlite3.Row | None:
        """Atomically claim an approved command so a rapid second click cannot run it twice."""
        with self.connect() as connection:
            claimed = connection.execute(
                "UPDATE cmd_audit SET executed_at=strftime('%s','now') "
                "WHERE proposal_id=? AND status='approved' AND executed_at IS NULL",
                (proposal_id,),
            )
            if claimed.rowcount != 1:
                return None
            return connection.execute("SELECT * FROM cmd_audit WHERE proposal_id=?", (proposal_id,)).fetchone()

    def finish_command(self, proposal_id: str, exit_code: int, stdout: str, stderr: str) -> None:
        """Persist command completion details and output tails."""
        with self.connect() as connection:
            connection.execute("""UPDATE cmd_audit SET status=?,exit_code=?,stdout_tail=?,stderr_tail=?,executed_at=strftime('%s','now')
                WHERE proposal_id=? AND status='approved' AND executed_at IS NOT NULL""",
                ("executed" if exit_code == 0 else "failed",exit_code,stdout[-4000:],stderr[-4000:],proposal_id))
