"""SQLite persistence for FileBot's index, event queue, and command audit."""

from __future__ import annotations

import logging
import re
import sqlite3
import hashlib
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal


log = logging.getLogger(__name__)

DirtyOperation = Literal["upsert", "delete"]

# A file that keeps failing to index is retried this many times, then parked.
MAX_DIRTY_ATTEMPTS = 12

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    snippet, file_name, tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, snippet, file_name)
    VALUES (new.rowid, new.snippet,
            COALESCE((SELECT file_name FROM files WHERE file_id = new.file_id), ''));
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.rowid;
END;
"""

_STOPWORDS = frozenset("""a an and are as at be by for from how i in is it me my of on or
the to was what when where which who with find show search look get open file files""".split())

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
        self.fts_enabled = False
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
                self._initialize_fts(connection)
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not initialize FileBot database at {self.path}: {exc}") from exc

    def _initialize_fts(self, connection: sqlite3.Connection) -> None:
        """Create the FTS5 lexical index and backfill it once; fall back to LIKE if unavailable."""
        existed = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='chunks_fts'").fetchone() is not None
        try:
            connection.executescript(_FTS_SCHEMA)
        except sqlite3.OperationalError as exc:
            log.warning("SQLite FTS5 is unavailable (%s); using slower LIKE lexical search", exc)
            self.fts_enabled = False
            return
        self.fts_enabled = True
        chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        indexed = int(connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0])
        if not existed or chunks != indexed:
            if existed:
                log.warning("Lexical index out of sync (%d chunks, %d entries); rebuilding", chunks, indexed)
            connection.execute("DELETE FROM chunks_fts")
            connection.execute(
                "INSERT INTO chunks_fts(rowid, snippet, file_name) "
                "SELECT c.rowid, c.snippet, f.file_name FROM chunks c JOIN files f USING(file_id)")

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
                if int(revision["attempts"]) + 1 >= MAX_DIRTY_ATTEMPTS:
                    self._park_failed_file(connection, normalized, error)
                    connection.execute("DELETE FROM dirty_queue WHERE path=? AND queued_at=?",
                                       (normalized, float(revision["queued_at"])))
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

    @staticmethod
    def _park_failed_file(connection: sqlite3.Connection, path: str, error: str) -> None:
        """Stop retrying a file that keeps failing; a later change re-queues it.

        A previously indexed file keeps its old searchable content; only its
        size/mtime are recorded so rescans do not retry an unchanged bad file.
        """
        try:
            stat = os.stat(path)
            size, mtime = stat.st_size, stat.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        name = Path(path).name
        connection.execute(
            """INSERT INTO files(path,file_name,ext,size_bytes,mtime,status,error)
               VALUES(?,?,?,?,?,'failed',?)
               ON CONFLICT(path) DO UPDATE SET size_bytes=excluded.size_bytes,
                   mtime=excluded.mtime, error=excluded.error""",
            (path, name, Path(name).suffix.lower(), size, mtime, error[:2000]))
        log.warning("Giving up indexing %s after %d attempts: %s", path, MAX_DIRTY_ATTEMPTS, error[:200])

    def unchanged_paths(self, candidates: list[tuple[str, int, float]]) -> set[str]:
        """Return candidate paths whose stored size and mtime still match (no reindex needed).

        Indexed files qualify when their stored hash is current; files parked as
        ``failed`` qualify so an unchanged bad file is not retried on every scan.
        """
        unchanged: set[str] = set()
        for start in range(0, len(candidates), 500):
            batch = candidates[start:start + 500]
            marks = ",".join("?" for _ in batch)
            with self.connect() as connection:
                stored = {str(row["path"]): row for row in connection.execute(
                    f"SELECT path,size_bytes,mtime,status,content_hash FROM files WHERE path IN ({marks})",
                    [path for path, _, _ in batch])}
            for path, size, mtime in batch:
                row = stored.get(path)
                if row is None or int(row["size_bytes"]) != size or abs(float(row["mtime"]) - mtime) > 0.001:
                    continue
                if row["status"] == "failed" or (row["status"] == "indexed" and row["content_hash"]):
                    unchanged.add(path)
        return unchanged

    def iter_file_paths(self, after_id: int = 0, limit: int = 1000) -> list[tuple[int, str]]:
        """Page through stored file ids and paths in id order (for stale-entry sweeps)."""
        with self.connect() as connection:
            return [(int(row[0]), str(row[1])) for row in connection.execute(
                "SELECT file_id, path FROM files WHERE file_id>? ORDER BY file_id LIMIT ?",
                (after_id, limit))]

    def get_file(self, path: str | Path) -> sqlite3.Row | None:
        """Return indexed file metadata for a normalized path."""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM files WHERE path=?", (str(Path(path).resolve()),)).fetchone()

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

    def requeue_all_files(self) -> int:
        """Force every indexed file to be re-parsed and re-embedded; return the count.

        Clearing the stored hash defeats the indexer's "unchanged" shortcut, so the
        queued upserts rebuild vectors (used when the embedding model changes).
        """
        with self.connect() as connection:
            paths = [str(row[0]) for row in connection.execute(
                "SELECT path FROM files WHERE status='indexed'")]
            connection.execute("UPDATE files SET content_hash='' WHERE status='indexed'")
        for start in range(0, len(paths), 500):
            self.enqueue_dirty_many([(p, "upsert") for p in paths[start:start + 500]])
        return len(paths)

    def vector_ids_for_file(self, path: str | Path) -> list[str]:
        """Return the committed vector identifiers for a normalized file path."""
        normalized = str(Path(path).expanduser().resolve())
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.chunk_id FROM chunks c JOIN files f USING(file_id) WHERE f.path=?",
                (normalized,),
            )
            return [str(row[0]) for row in rows]

    @staticmethod
    def _prefix_clause(path_prefixes: list[str] | None) -> tuple[str, list[object]]:
        """SQL restricting ``f.path`` to directories; prefixes end with a path separator."""
        if path_prefixes is None:
            return "", []
        if not path_prefixes:
            return " AND 0", []
        parts = " OR ".join("substr(f.path, 1, ?) COLLATE NOCASE = ?" for _ in path_prefixes)
        params: list[object] = []
        for prefix in path_prefixes:
            params += [len(prefix), prefix]
        return f" AND ({parts})", params

    @staticmethod
    def fts_query(query: str) -> str:
        """Turn free text into a safe OR-of-terms FTS5 query (stopwords dropped when possible)."""
        tokens = re.findall(r"\w+", query)
        useful = [token for token in tokens if token.lower() not in _STOPWORDS] or tokens
        return " OR ".join('"' + token.replace('"', '""') + '"' for token in useful[:32])

    def search_lexical(self, query: str, limit: int = 20,
                       path_prefixes: list[str] | None = None) -> list[sqlite3.Row]:
        """BM25-ranked lexical search (FTS5), or a LIKE scan when FTS5 is unavailable.

        ``path_prefixes`` (each ending in a path separator) restricts results to
        those directories; ``None`` means no restriction.
        """
        scope, scope_params = self._prefix_clause(path_prefixes)
        with self.connect() as connection:
            if self.fts_enabled:
                match = self.fts_query(query)
                if not match:
                    return []
                return list(connection.execute(
                    f"""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading
                        FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid
                        JOIN files f ON f.file_id = c.file_id
                        WHERE chunks_fts MATCH ? AND f.status='indexed'{scope}
                        ORDER BY bm25(chunks_fts, 1.0, 2.0) LIMIT ?""",
                    (match, *scope_params, limit)))
            terms = [f"%{term}%" for term in query.split() if term]
            if not terms:
                return []
            clause = " OR ".join("c.snippet LIKE ?" for _ in terms)
            return list(connection.execute(
                f"""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading
                    FROM chunks c JOIN files f USING(file_id)
                    WHERE ({clause}) AND f.status='indexed'{scope} LIMIT ?""",
                (*terms, *scope_params, limit)))

    def search_exact_token(self, token: str, limit: int = 2000,
                           path_prefixes: list[str] | None = None) -> list[sqlite3.Row]:
        """Candidate chunks for a short identifier; callers verify word boundaries.

        Tokens made only of word characters use the FTS index. Tokens with
        punctuation (``c#``) fall back to a bounded LIKE scan.
        """
        scope, scope_params = self._prefix_clause(path_prefixes)
        with self.connect() as connection:
            if self.fts_enabled and re.fullmatch(r"\w+", token):
                return list(connection.execute(
                    f"""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading
                        FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid
                        JOIN files f ON f.file_id = c.file_id
                        WHERE chunks_fts MATCH ? AND f.status='indexed'{scope}
                        ORDER BY bm25(chunks_fts, 1.0, 2.0) LIMIT ?""",
                    ('"' + token + '"', *scope_params, limit)))
            like = "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            return list(connection.execute(
                f"""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading
                    FROM chunks c JOIN files f USING(file_id)
                    WHERE (c.snippet LIKE ? ESCAPE '\\' OR f.file_name LIKE ? ESCAPE '\\')
                    AND f.status='indexed'{scope} LIMIT ?""",
                (like, like, *scope_params, limit)))

    def chunks_by_ids(self, chunk_ids: list[str],
                      path_prefixes: list[str] | None = None) -> list[sqlite3.Row]:
        """Fetch display rows for vector hits, dropping stale ids and out-of-scope paths."""
        scope, scope_params = self._prefix_clause(path_prefixes)
        rows: list[sqlite3.Row] = []
        for start in range(0, len(chunk_ids), 500):
            batch = chunk_ids[start:start + 500]
            marks = ",".join("?" for _ in batch)
            with self.connect() as connection:
                rows += list(connection.execute(
                    f"""SELECT c.chunk_id,c.snippet,f.path,f.file_name,c.page_or_line,c.heading
                        FROM chunks c JOIN files f USING(file_id)
                        WHERE c.chunk_id IN ({marks}) AND f.status='indexed'{scope}""",
                    (*batch, *scope_params)))
        return rows

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
