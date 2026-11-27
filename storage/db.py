"""SQLite persistence for FileBot's index, event queue, and command audit."""

from __future__ import annotations

import sqlite3
import hashlib
import os
import re
import secrets
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
    available_at REAL NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_turns (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_session ON chat_turns(session_key, turn_id);

CREATE TABLE IF NOT EXISTS cmd_audit (
    proposal_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL,
    shell TEXT NOT NULL DEFAULT 'cmd' CHECK(shell IN ('cmd', 'powershell')),
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

CREATE TABLE IF NOT EXISTS file_delete_audit (
    token TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    file_size INTEGER NOT NULL CHECK(file_size >= 0),
    file_mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','executed','cancelled','expired','failed')),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    completed_at REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_delete_status ON file_delete_audit(status, created_at);
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
        """Create or migrate the schema and enable persistent write-ahead logging."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
                columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(dirty_queue)")}
                if "available_at" not in columns:
                    connection.execute("ALTER TABLE dirty_queue ADD COLUMN available_at REAL NOT NULL DEFAULT 0")
                command_columns = {
                    str(row["name"]) for row in connection.execute("PRAGMA table_info(cmd_audit)")
                }
                if "shell" not in command_columns:
                    connection.execute(
                        "ALTER TABLE cmd_audit ADD COLUMN shell TEXT NOT NULL DEFAULT 'cmd' "
                        "CHECK(shell IN ('cmd', 'powershell'))"
                    )
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not initialize FileBot database at {self.path}: {exc}") from exc

    def enqueue_dirty(self, path: str | Path, operation: DirtyOperation) -> None:
        """Insert or replace a debounced filesystem change in ``dirty_queue``."""
        self.enqueue_dirty_many([(path, operation)])

    def enqueue_dirty_many(self, events: list[tuple[str | Path, DirtyOperation]]) -> None:
        """Atomically insert a bounded batch of filesystem changes."""
        if not events:
            return
        normalized: list[tuple[str, DirtyOperation, float]] = []
        for path, operation in events:
            if operation not in ("upsert", "delete"):
                raise ValueError(f"Unsupported dirty operation: {operation!r}")
            normalized.append((str(Path(path).expanduser().resolve()), operation,
                               datetime.now(timezone.utc).timestamp()))
        try:
            with self.connect() as connection:
                connection.executemany(
                    """INSERT INTO dirty_queue(path, event_type, queued_at, available_at, attempts, last_error)
                       VALUES (?, ?, ?, ?, 0, NULL)
                       ON CONFLICT(path) DO UPDATE SET event_type=excluded.event_type,
                           queued_at=MAX(excluded.queued_at, dirty_queue.queued_at + 0.000001),
                           available_at=excluded.available_at, attempts=0, last_error=NULL""",
                    [(path, operation, queued_at, queued_at) for path, operation, queued_at in normalized],
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not queue filesystem event batch: {exc}") from exc

    def pending_dirty(self, limit: int = 100) -> list[sqlite3.Row]:
        """Return oldest queued filesystem changes without removing them."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        try:
            with self.connect() as connection:
                return list(connection.execute(
                    "SELECT path, event_type AS operation, queued_at, attempts, last_error "
                    "FROM dirty_queue WHERE available_at <= ? ORDER BY queued_at LIMIT ?",
                    (datetime.now(timezone.utc).timestamp(), limit),
                ))
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not read dirty queue: {exc}") from exc

    def dirty_record(self, path: str | Path) -> sqlite3.Row | None:
        """Return queue state, including deferred retry details, for diagnostics."""
        normalized = str(Path(path).expanduser().resolve())
        try:
            with self.connect() as connection:
                return connection.execute(
                    "SELECT path, event_type AS operation, queued_at, available_at, attempts, last_error "
                    "FROM dirty_queue WHERE path=?", (normalized,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not read queued path {normalized}: {exc}") from exc

    def remove_dirty(self, path: str | Path, queued_at: float | None = None) -> None:
        """Acknowledge an event, optionally only if it is still the same revision."""
        normalized = str(Path(path).expanduser().resolve())
        try:
            with self.connect() as connection:
                if queued_at is None:
                    connection.execute("DELETE FROM dirty_queue WHERE path = ?", (normalized,))
                else:
                    connection.execute("DELETE FROM dirty_queue WHERE path = ? AND queued_at = ?",
                                       (normalized, queued_at))
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not remove queued path {normalized}: {exc}") from exc

    def record_dirty_error(self, path: str | Path, error: str, queued_at: float | None = None) -> None:
        """Record a failure and defer retry, without changing a newer revision.

        Delays grow from five seconds to at most one hour. A subsequent file
        event resets ``available_at`` so fresh content is indexed promptly.
        """
        normalized = str(Path(path).expanduser().resolve())
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                revision = connection.execute(
                    "SELECT queued_at, attempts FROM dirty_queue WHERE path=?", (normalized,),
                ).fetchone()
                if revision is None or (queued_at is not None and float(revision["queued_at"]) != queued_at):
                    return
                delay = min(3600.0, 5.0 * (2 ** min(int(revision["attempts"]), 10)))
                connection.execute(
                    "UPDATE dirty_queue SET attempts=attempts+1, last_error=?, available_at=? "
                    "WHERE path=? AND queued_at=?",
                    (error[:2000], datetime.now(timezone.utc).timestamp() + delay,
                     normalized, float(revision["queued_at"])),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not record queue error for {normalized}: {exc}") from exc

    def get_file(self, path: str | Path) -> sqlite3.Row | None:
        """Return indexed file metadata for a normalized path."""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM files WHERE path=?", (str(Path(path).resolve()),)).fetchone()

    def find_indexed_by_name(self, name: str) -> list[sqlite3.Row]:
        """Find indexed files by exact basename, returning their best snippet."""
        file_name = Path(name).name
        with self.connect() as connection:
            return list(connection.execute(
                """SELECT f.path,f.file_name,c.snippet,c.page_or_line,c.heading
                   FROM files f LEFT JOIN chunks c ON c.file_id=f.file_id AND c.chunk_index=0
                   WHERE f.status='indexed' AND f.file_name=? COLLATE NOCASE
                   ORDER BY f.path COLLATE NOCASE""",
                (file_name,),
            ))

    def record_file_open(self, path: str | Path) -> None:
        """Increment the indexed file's open signal after a successful open."""
        normalized = str(Path(path).expanduser().resolve())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO file_signals(file_id,open_count,last_opened)
                   SELECT file_id,1,strftime('%s','now') FROM files WHERE path=?
                   ON CONFLICT(file_id) DO UPDATE SET
                     open_count=open_count+1,last_opened=excluded.last_opened""",
                (normalized,),
            )

    def record_file_feedback(self, path: str | Path, vote: str) -> bool:
        """Record a positive or negative retrieval signal for an indexed file."""
        if vote not in {"up", "down"}:
            raise ValueError("vote must be 'up' or 'down'")
        normalized = str(Path(path).expanduser().resolve())
        column = "upvotes" if vote == "up" else "downvotes"
        with self.connect() as connection:
            row = connection.execute("SELECT file_id FROM files WHERE path=?", (normalized,)).fetchone()
            if row is None:
                return False
            connection.execute(
                f"""INSERT INTO file_signals(file_id,{column}) VALUES(?,1)
                    ON CONFLICT(file_id) DO UPDATE SET {column}={column}+1""",
                (int(row["file_id"]),),
            )
        return True

    def file_signal_counts(self, path: str | Path) -> tuple[int, int, int]:
        """Return the current open/up/down counts for one indexed path."""
        normalized = str(Path(path).expanduser().resolve())
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COALESCE(s.open_count,0),COALESCE(s.upvotes,0),COALESCE(s.downvotes,0)
                   FROM files f LEFT JOIN file_signals s USING(file_id) WHERE f.path=?""",
                (normalized,),
            ).fetchone()
        return (int(row[0]), int(row[1]), int(row[2])) if row is not None else (0, 0, 0)

    def recent_chat_turns(self, session_key: str, limit: int = 50) -> list[tuple[str, str]]:
        """Load a bounded local transcript in chronological order."""
        if not session_key or limit < 1:
            raise ValueError("a session key and positive chat history limit are required")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT user_text, assistant_text FROM chat_turns
                   WHERE session_key=? ORDER BY turn_id DESC LIMIT ?""",
                (session_key, limit),
            ).fetchall()
        return [(str(row[0]), str(row[1])) for row in reversed(rows)]

    def record_chat_turn(
        self, session_key: str, user_text: str, assistant_text: str, *, retain: int = 50,
    ) -> None:
        """Save one completed local conversation turn and prune older turns."""
        if not session_key or not user_text.strip() or not assistant_text.strip():
            raise ValueError("chat turns require a session key, user text, and assistant text")
        if retain < 1:
            raise ValueError("retain must be at least 1")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO chat_turns(session_key,user_text,assistant_text,created_at)
                   VALUES(?,?,?,?)""",
                (session_key, user_text, assistant_text, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                """DELETE FROM chat_turns WHERE session_key=? AND turn_id NOT IN (
                       SELECT turn_id FROM chat_turns WHERE session_key=?
                       ORDER BY turn_id DESC LIMIT ?
                   )""",
                (session_key, session_key, retain),
            )

    def set_glossary_entries(self, entries: dict[str, str]) -> None:
        """Persist validated shorthand expansions without removing user entries."""
        normalized: list[tuple[str, str]] = []
        for alias, expansion in entries.items():
            if not isinstance(alias, str) or not alias.strip() or len(alias) > 100:
                raise ValueError("glossary aliases must be non-empty strings of at most 100 characters")
            if not isinstance(expansion, str) or not expansion.strip() or len(expansion) > 500:
                raise ValueError("glossary expansions must be non-empty strings of at most 500 characters")
            normalized.append((alias.strip(), expansion.strip()))
        with self.connect() as connection:
            connection.executemany(
                """INSERT INTO glossary(alias,expansion) VALUES(?,?)
                   ON CONFLICT(alias) DO UPDATE SET expansion=excluded.expansion""",
                normalized,
            )

    def glossary_entries(self) -> list[tuple[str, str]]:
        """Return configured aliases in stable case-insensitive order."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT alias, expansion FROM glossary ORDER BY alias COLLATE NOCASE"
            )
            return [(str(row[0]), str(row[1])) for row in rows]

    def replace_glossary_entries(self, entries: dict[str, str]) -> None:
        """Atomically replace the glossary after validating every mapping."""
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for alias, expansion in entries.items():
            if not isinstance(alias, str) or not alias.strip() or len(alias) > 100:
                raise ValueError("glossary aliases must be non-empty strings of at most 100 characters")
            if not isinstance(expansion, str) or not expansion.strip() or len(expansion) > 500:
                raise ValueError("glossary expansions must be non-empty strings of at most 500 characters")
            key = alias.strip().casefold()
            if key in seen:
                raise ValueError("glossary aliases must be unique, ignoring case")
            seen.add(key)
            normalized.append((alias.strip(), expansion.strip()))
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM glossary")
                connection.executemany(
                    "INSERT INTO glossary(alias, expansion) VALUES(?, ?)", normalized,
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not save glossary entries: {exc}") from exc

    def seed_glossary_entries(self, entries: dict[str, str]) -> None:
        """Seed YAML glossary defaults once; later UI edits remain authoritative."""
        normalized: list[tuple[str, str]] = []
        for alias, expansion in entries.items():
            if not isinstance(alias, str) or not alias.strip() or len(alias) > 100:
                raise ValueError("glossary aliases must be non-empty strings of at most 100 characters")
            if not isinstance(expansion, str) or not expansion.strip() or len(expansion) > 500:
                raise ValueError("glossary expansions must be non-empty strings of at most 500 characters")
            normalized.append((alias.strip(), expansion.strip()))
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                seeded = connection.execute(
                    "SELECT value FROM app_state WHERE key='glossary_seeded'"
                ).fetchone()
                if seeded is not None:
                    return
                connection.executemany(
                    "INSERT INTO glossary(alias, expansion) VALUES(?, ?) "
                    "ON CONFLICT(alias) DO NOTHING", normalized,
                )
                connection.execute(
                    "INSERT INTO app_state(key, value) VALUES('glossary_seeded', '1')"
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not seed glossary entries: {exc}") from exc

    def expand_glossary(self, query: str) -> str:
        """Expand configured aliases at word boundaries in a search query."""
        with self.connect() as connection:
            entries = list(connection.execute(
                "SELECT alias,expansion FROM glossary ORDER BY length(alias) DESC,alias COLLATE NOCASE"
            ))
        expanded = query
        for row in entries:
            alias = str(row["alias"]).strip()
            if alias:
                expanded = re.sub(
                    rf"(?<!\w){re.escape(alias)}(?!\w)",
                    lambda _match, value=str(row["expansion"]): value,
                    expanded,
                    flags=re.IGNORECASE,
                )
        return expanded

    def paths_under(self, directory: str | Path) -> list[str]:
        """List indexed and queued file paths beneath a directory."""
        root = str(Path(directory).expanduser().resolve()).rstrip("\\/") + os.sep
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT path FROM files WHERE substr(path, 1, ?) COLLATE NOCASE = ? "
                    "UNION SELECT path FROM dirty_queue WHERE substr(path, 1, ?) COLLATE NOCASE = ?",
                    (len(root), root, len(root), root),
                )
                return [str(row[0]) for row in rows]
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not list indexed paths beneath {directory}: {exc}") from exc

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
                chunk_id = hashlib.sha256(f"{normalized}:{content_hash}:{index}".encode("utf-8")).hexdigest()
                connection.execute("""INSERT INTO chunks(chunk_id,file_id,chunk_index,page_or_line,heading,snippet,token_count,vector_id)
                    VALUES(?,?,?,?,?,?,?,?)""", (chunk_id,file_id,index,item.get("page_or_line"),item.get("heading"),
                    str(item.get("text", "")),len(str(item.get("text", "")).split()),chunk_id))
            return file_id

    def delete_file(self, path: str | Path) -> None:
        """Remove an indexed file and its dependent chunks and signals."""
        with self.connect() as connection:
            connection.execute("DELETE FROM files WHERE path=?", (str(Path(path).resolve()),))

    def vector_ids_for_file(self, path: str | Path) -> list[str]:
        """Return the committed vector identifiers for a normalized file path."""
        normalized = str(Path(path).expanduser().resolve())
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.chunk_id FROM chunks c JOIN files f USING(file_id) WHERE f.path=?",
                (normalized,),
            )
            return [str(row[0]) for row in rows]

    def all_chunks(self) -> list[sqlite3.Row]:
        """Return chunk corpus records for the in-memory BM25 index."""
        with self.connect() as connection:
            return list(connection.execute("""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading,
                COALESCE(s.open_count,0) AS open_count,COALESCE(s.upvotes,0) AS upvotes,
                COALESCE(s.downvotes,0) AS downvotes
                FROM chunks c JOIN files f USING(file_id) LEFT JOIN file_signals s USING(file_id)
                WHERE f.status='indexed' ORDER BY f.file_id,c.chunk_index"""))

    def indexed_file_chunks(
        self, path: str | Path, limit: int = 16, chars_per_chunk: int = 1200,
    ) -> list[sqlite3.Row]:
        """Return bounded stored chunks for one currently indexed path."""
        if limit < 1 or not 1 <= chars_per_chunk <= 12_000:
            raise ValueError("chunk limit or excerpt size is outside supported bounds")
        normalized = str(Path(path).expanduser().resolve())
        with self.connect() as connection:
            return list(connection.execute(
                """SELECT substr(c.snippet,1,?) AS snippet,c.page_or_line,c.heading FROM chunks c
                   JOIN files f USING(file_id)
                   WHERE f.path=? AND f.status='indexed'
                   ORDER BY c.chunk_index LIMIT ?""",
                (chars_per_chunk, normalized, limit),
            ))

    def search_lexical(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """Fallback lexical search using SQLite LIKE when BM25 is not active."""
        terms = [f"%{term}%" for term in query.split() if term]
        if not terms:
            return []
        clause = " OR ".join("c.snippet LIKE ?" for _ in terms)
        with self.connect() as connection:
            return list(connection.execute(f"""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading,
                COALESCE(s.open_count,0) AS open_count,COALESCE(s.upvotes,0) AS upvotes,
                COALESCE(s.downvotes,0) AS downvotes
                FROM chunks c JOIN files f USING(file_id) LEFT JOIN file_signals s USING(file_id)
                WHERE {clause} LIMIT ?""", (*terms, limit)))

    def stage_command(
        self, command: str, cwd: str, reason: str, risk: str, status: str = "pending",
        *, shell: str = "cmd",
    ) -> str:
        """Create an immutable audit proposal and return its identifier."""
        if shell not in {"cmd", "powershell"}:
            raise ValueError("shell must be 'cmd' or 'powershell'")
        proposal_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("""INSERT INTO cmd_audit(proposal_id,command,shell,cwd,reason,risk_level,status,created_at)
                VALUES(?,?,?,?,?,?,?,strftime('%s','now'))""", (proposal_id,command,shell,cwd,reason,risk,status))
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

    def recover_command_states(self) -> tuple[int, int]:
        """Repair approval rows left behind by a previous process crash.

        Approved commands not yet claimed return to pending and require a fresh
        click. Claimed commands are marked failed with an uncertain outcome and
        are never replayed automatically.
        """
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                unclaimed = connection.execute(
                    "UPDATE cmd_audit SET status='pending', approved_at=NULL "
                    "WHERE status='approved' AND executed_at IS NULL",
                ).rowcount
                interrupted = connection.execute(
                    "UPDATE cmd_audit SET status='failed', exit_code=125, "
                    "error='FileBot stopped during command execution; outcome may be partial' "
                    "WHERE status='approved' AND executed_at IS NOT NULL",
                ).rowcount
                return unclaimed, interrupted
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not recover command audit state: {exc}") from exc

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

    def stage_file_delete(
        self, path: str | Path, *, file_size: int, file_mtime_ns: int, lifetime_seconds: int = 600,
    ) -> str:
        """Stage one exact file for deletion after a short-lived chat confirmation token."""
        if file_size < 0 or not 30 <= lifetime_seconds <= 900:
            raise ValueError("Invalid file-delete audit bounds")
        token = secrets.token_hex(4).upper()
        now = datetime.now(timezone.utc).timestamp()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO file_delete_audit
                   (token,path,file_size,file_mtime_ns,status,created_at,expires_at)
                   VALUES(?,?,?,?,'pending',?,?)""",
                (token, str(Path(path).expanduser().resolve()), file_size, file_mtime_ns,
                 now, now + lifetime_seconds),
            )
        return token

    def claim_file_delete(self, token: str) -> sqlite3.Row | None:
        """Claim one pending token exactly once; expired requests cannot run."""
        normalized = token.strip().upper()
        now = datetime.now(timezone.utc).timestamp()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM file_delete_audit WHERE token=? AND status='pending'", (normalized,),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE file_delete_audit SET status='expired',completed_at=? WHERE token=? AND status='pending'",
                    (now, normalized),
                )
                return None
            changed = connection.execute(
                "UPDATE file_delete_audit SET status='running' WHERE token=? AND status='pending'",
                (normalized,),
            ).rowcount
            return connection.execute(
                "SELECT * FROM file_delete_audit WHERE token=?", (normalized,),
            ).fetchone() if changed == 1 else None

    def cancel_file_delete(self, token: str) -> bool:
        """Cancel a pending deletion without touching the named file."""
        normalized = token.strip().upper()
        now = datetime.now(timezone.utc).timestamp()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE file_delete_audit SET status='expired',completed_at=?
                   WHERE token=? AND status='pending' AND expires_at<=?""",
                (now, normalized, now),
            )
            changed = connection.execute(
                """UPDATE file_delete_audit SET status='cancelled',completed_at=?
                   WHERE token=? AND status='pending' AND expires_at>?""",
                (now, normalized, now),
            )
            return changed.rowcount == 1

    def finish_file_delete(self, token: str, *, error: str | None = None) -> None:
        """Record the result of the claimed Recycle Bin operation."""
        status = "failed" if error else "executed"
        with self.connect() as connection:
            connection.execute(
                """UPDATE file_delete_audit SET status=?,completed_at=?,error=?
                   WHERE token=? AND status='running'""",
                (status, datetime.now(timezone.utc).timestamp(), error[:1000] if error else None,
                 token.strip().upper()),
            )

    def recover_file_deletes(self) -> tuple[int, int]:
        """Expire old confirmations and mark interrupted delete attempts as failed."""
        now = datetime.now(timezone.utc).timestamp()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                """UPDATE file_delete_audit SET status='expired',completed_at=?
                   WHERE status='pending' AND expires_at<=?""", (now, now),
            ).rowcount
            interrupted = connection.execute(
                """UPDATE file_delete_audit SET status='failed',completed_at=?,
                   error='FileBot stopped during the Recycle Bin operation; verify the file location'
                   WHERE status='running'""", (now,),
            ).rowcount
            return expired, interrupted
