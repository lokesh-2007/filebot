//! Connection-per-operation SQLite persistence compatible with FileBot's schema.
use rusqlite::{Connection, OptionalExtension, params};
use std::{
    fs,
    path::{Component, Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
use thiserror::Error;
use uuid::Uuid;

const SCHEMA: &str = r#"
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
    UNIQUE(file_id, chunk_index), CHECK(length(chunk_id) > 0)
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
    alias TEXT PRIMARY KEY COLLATE NOCASE, expansion TEXT NOT NULL
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
"#;

/// Durable file change operation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DirtyOperation {
    Upsert,
    Delete,
}

impl DirtyOperation {
    fn as_str(self) -> &'static str {
        match self {
            Self::Upsert => "upsert",
            Self::Delete => "delete",
        }
    }
}

/// File system event persisted until an index worker acknowledges it.
#[derive(Clone, Debug, PartialEq)]
pub struct DirtyEvent {
    pub path: String,
    pub operation: DirtyOperation,
    pub queued_at: f64,
    pub attempts: u32,
    pub last_error: Option<String>,
}

/// Persisted command proposal and its decision state.
#[derive(Clone, Debug, PartialEq)]
pub struct CommandRecord {
    pub proposal_id: String,
    pub command: String,
    pub cwd: String,
    pub reason: String,
    pub risk_level: String,
    pub status: String,
    pub exit_code: Option<i32>,
    pub stdout_tail: Option<String>,
    pub stderr_tail: Option<String>,
    pub executed_at: Option<f64>,
}

#[derive(Debug, Error)]
pub enum StorageError {
    #[error("invalid database path: {0}")]
    InvalidPath(String),
    #[error("database operation failed: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("filesystem operation failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("invalid value: {0}")]
    InvalidValue(String),
}

/// SQLite WAL database using fresh connections safe for independent worker threads.
#[derive(Clone, Debug)]
pub struct Database {
    path: PathBuf,
    busy_timeout_ms: u64,
}

impl Database {
    /// Construct a database handle; initialization is explicit and idempotent.
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            busy_timeout_ms: 5_000,
        }
    }

    /// Set SQLite's lock timeout, in milliseconds.
    pub fn with_busy_timeout(mut self, timeout_ms: u64) -> Result<Self, StorageError> {
        if timeout_ms == 0 {
            return Err(StorageError::InvalidValue(
                "busy timeout must be positive".into(),
            ));
        }
        self.busy_timeout_ms = timeout_ms;
        Ok(self)
    }

    /// Create/migrate schema and enable WAL plus foreign-key enforcement.
    pub fn initialize(&self) -> Result<(), StorageError> {
        if let Some(parent) = self
            .path
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
        {
            fs::create_dir_all(parent)?;
        }
        let connection = self.connect()?;
        connection.pragma_update(None, "journal_mode", "WAL")?;
        let tx = connection.unchecked_transaction()?;
        tx.execute_batch(SCHEMA)?;
        let mut statement = tx.prepare("PRAGMA table_info(dirty_queue)")?;
        let columns = statement.query_map([], |row| row.get::<_, String>(1))?;
        let mut has_available_at = false;
        for column in columns {
            if column? == "available_at" {
                has_available_at = true;
            }
        }
        if !has_available_at {
            tx.execute(
                "ALTER TABLE dirty_queue ADD COLUMN available_at REAL NOT NULL DEFAULT 0",
                [],
            )?;
        }
        drop(statement);
        tx.commit()?;
        Ok(())
    }

    fn connect(&self) -> Result<Connection, StorageError> {
        let connection = Connection::open(&self.path)?;
        connection.busy_timeout(std::time::Duration::from_millis(self.busy_timeout_ms))?;
        connection.pragma_update(None, "foreign_keys", "ON")?;
        Ok(connection)
    }

    /// Coalesce a file event into a single upsert/delete entry.
    pub fn enqueue_dirty(
        &self,
        path: impl AsRef<Path>,
        operation: DirtyOperation,
    ) -> Result<(), StorageError> {
        let normalized = normalize_path(path.as_ref())?;
        let now = unix_time()?;
        let connection = self.connect()?;
        connection.execute(
            "INSERT INTO dirty_queue(path,event_type,queued_at,available_at,attempts,last_error) \
             VALUES(?1,?2,?3,?3,0,NULL) ON CONFLICT(path) DO UPDATE SET \
             event_type=excluded.event_type, queued_at=MAX(excluded.queued_at,dirty_queue.queued_at+0.000001), \
             available_at=excluded.available_at, attempts=0, last_error=NULL",
            params![normalized, operation.as_str(), now],
        )?;
        Ok(())
    }

    /// Return ready events in oldest-first order without acknowledging them.
    pub fn pending_dirty(&self, limit: usize) -> Result<Vec<DirtyEvent>, StorageError> {
        if limit == 0 {
            return Err(StorageError::InvalidValue("limit must be positive".into()));
        }
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT path,event_type,queued_at,attempts,last_error FROM dirty_queue \
             WHERE available_at<=?1 ORDER BY queued_at LIMIT ?2",
        )?;
        let rows = statement.query_map(params![unix_time()?, limit as i64], |row| {
            let operation: String = row.get(1)?;
            Ok(DirtyEvent {
                path: row.get(0)?,
                operation: if operation == "delete" {
                    DirtyOperation::Delete
                } else {
                    DirtyOperation::Upsert
                },
                queued_at: row.get(2)?,
                attempts: row.get::<_, i64>(3)?.max(0) as u32,
                last_error: row.get(4)?,
            })
        })?;
        rows.collect::<Result<Vec<_>, _>>().map_err(Into::into)
    }

    /// Remove a queue item only if it is still the revision that was processed.
    pub fn acknowledge_dirty(
        &self,
        path: impl AsRef<Path>,
        queued_at: f64,
    ) -> Result<bool, StorageError> {
        let normalized = normalize_path(path.as_ref())?;
        let connection = self.connect()?;
        Ok(connection.execute(
            "DELETE FROM dirty_queue WHERE path=?1 AND queued_at=?2",
            params![normalized, queued_at],
        )? == 1)
    }

    /// Record a failure using bounded exponential retry backoff, preserving newer events.
    pub fn record_dirty_error(
        &self,
        path: impl AsRef<Path>,
        queued_at: f64,
        error: &str,
    ) -> Result<bool, StorageError> {
        let normalized = normalize_path(path.as_ref())?;
        let mut connection = self.connect()?;
        let tx = connection.transaction()?;
        let current = tx
            .query_row(
                "SELECT attempts FROM dirty_queue WHERE path=?1 AND queued_at=?2",
                params![normalized, queued_at],
                |row| row.get::<_, u32>(0),
            )
            .optional()?;
        let Some(attempts) = current else {
            return Ok(false);
        };
        let delay = 5.0_f64 * 2_f64.powi(attempts.min(10) as i32);
        let changed = tx.execute(
            "UPDATE dirty_queue SET attempts=attempts+1,last_error=?1,available_at=?2 \
             WHERE path=?3 AND queued_at=?4",
            params![
                error.chars().take(2000).collect::<String>(),
                unix_time()? + delay.min(3600.0),
                normalized,
                queued_at
            ],
        )?;
        tx.commit()?;
        Ok(changed == 1)
    }

    /// Stage a command proposal. This does not execute anything.
    pub fn stage_command(
        &self,
        command: &str,
        cwd: &str,
        reason: &str,
        risk_level: &str,
    ) -> Result<String, StorageError> {
        if !matches!(risk_level, "read_only" | "gated" | "blocked") {
            return Err(StorageError::InvalidValue(
                "unknown command risk level".into(),
            ));
        }
        let id = Uuid::new_v4().to_string();
        let status = if risk_level == "blocked" {
            "blocked"
        } else {
            "pending"
        };
        let connection = self.connect()?;
        connection.execute(
            "INSERT INTO cmd_audit(proposal_id,command,cwd,reason,risk_level,status,created_at) \
             VALUES(?1,?2,?3,?4,?5,?6,?7)",
            params![id, command, cwd, reason, risk_level, status, unix_time()?],
        )?;
        Ok(id)
    }

    /// Apply one explicit decision to a proposal that is still pending.
    pub fn decide_command(&self, proposal_id: &str, approve: bool) -> Result<bool, StorageError> {
        let connection = self.connect()?;
        let status = if approve { "approved" } else { "rejected" };
        Ok(connection.execute(
            "UPDATE cmd_audit SET status=?1,approved_at=?2 WHERE proposal_id=?3 AND status='pending'",
            params![status, unix_time()?, proposal_id],
        )? == 1)
    }

    /// Atomically mark an approved proposal as claimed, preventing duplicate execution.
    pub fn claim_approved_command(
        &self,
        proposal_id: &str,
    ) -> Result<Option<CommandRecord>, StorageError> {
        let mut connection = self.connect()?;
        let tx = connection.transaction()?;
        let claimed = tx.execute(
            "UPDATE cmd_audit SET executed_at=?1 WHERE proposal_id=?2 AND status='approved' AND executed_at IS NULL",
            params![unix_time()?, proposal_id],
        )?;
        if claimed != 1 {
            return Ok(None);
        }
        let record = tx.query_row(
            "SELECT proposal_id,command,cwd,reason,risk_level,status,exit_code,stdout_tail,stderr_tail,executed_at \
             FROM cmd_audit WHERE proposal_id=?1", [proposal_id], command_from_row,
        )?;
        tx.commit()?;
        Ok(Some(record))
    }

    /// Persist bounded stdout/stderr tails and a final process status.
    pub fn finish_command(
        &self,
        proposal_id: &str,
        exit_code: i32,
        stdout: &str,
        stderr: &str,
    ) -> Result<bool, StorageError> {
        let connection = self.connect()?;
        let status = if exit_code == 0 { "executed" } else { "failed" };
        Ok(connection.execute(
            "UPDATE cmd_audit SET status=?1,exit_code=?2,stdout_tail=?3,stderr_tail=?4 \
             WHERE proposal_id=?5 AND status='approved' AND executed_at IS NOT NULL",
            params![
                status,
                exit_code,
                tail_chars(stdout, 4000),
                tail_chars(stderr, 4000),
                proposal_id
            ],
        )? == 1)
    }

    /// Recover proposals after a crash; interrupted claims are never auto-replayed.
    pub fn recover_command_states(&self) -> Result<(usize, usize), StorageError> {
        let mut connection = self.connect()?;
        let tx = connection.transaction()?;
        let needs_reapproval = tx.execute(
            "UPDATE cmd_audit SET status='pending',approved_at=NULL WHERE status='approved' AND executed_at IS NULL", [],
        )?;
        let interrupted = tx.execute(
            "UPDATE cmd_audit SET status='failed',exit_code=125, \
             error='FileBot stopped during command execution; outcome may be partial' \
             WHERE status='approved' AND executed_at IS NOT NULL",
            [],
        )?;
        tx.commit()?;
        Ok((needs_reapproval, interrupted))
    }

    /// Retrieve an audit row for display or support diagnostics.
    pub fn command_record(&self, proposal_id: &str) -> Result<Option<CommandRecord>, StorageError> {
        let connection = self.connect()?;
        connection.query_row(
            "SELECT proposal_id,command,cwd,reason,risk_level,status,exit_code,stdout_tail,stderr_tail,executed_at \
             FROM cmd_audit WHERE proposal_id=?1", [proposal_id], command_from_row,
        ).optional().map_err(Into::into)
    }

    /// Return proposals that still require a visible user decision.
    pub fn pending_commands(&self) -> Result<Vec<CommandRecord>, StorageError> {
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT proposal_id,command,cwd,reason,risk_level,status,exit_code,stdout_tail,stderr_tail,executed_at \
             FROM cmd_audit WHERE status='pending' ORDER BY created_at",
        )?;
        let rows = statement.query_map([], command_from_row)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(Into::into)
    }
}

fn command_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<CommandRecord> {
    Ok(CommandRecord {
        proposal_id: row.get(0)?,
        command: row.get(1)?,
        cwd: row.get(2)?,
        reason: row.get(3)?,
        risk_level: row.get(4)?,
        status: row.get(5)?,
        exit_code: row.get(6)?,
        stdout_tail: row.get(7)?,
        stderr_tail: row.get(8)?,
        executed_at: row.get(9)?,
    })
}

fn unix_time() -> Result<f64, StorageError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .map_err(|_| StorageError::InvalidValue("system clock predates Unix epoch".into()))
}

fn normalize_path(path: &Path) -> Result<String, StorageError> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    if !normalized.is_absolute() {
        return Err(StorageError::InvalidPath(path.display().to_string()));
    }
    Ok(normalized.to_string_lossy().into_owned())
}

fn tail_chars(value: &str, limit: usize) -> String {
    let chars: Vec<char> = value.chars().rev().take(limit).collect();
    chars.into_iter().rev().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database() -> (tempfile::TempDir, Database) {
        let dir = tempfile::tempdir().expect("temporary directory");
        let db = Database::new(dir.path().join("nested/state.sqlite3"));
        db.initialize().expect("database initializes");
        (dir, db)
    }

    #[test]
    fn initializes_wal_schema_and_idempotent_migration() {
        let (_dir, db) = database();
        db.initialize().expect("initialize can run more than once");
        let connection = db.connect().expect("database connection");
        let journal: String = connection
            .pragma_query_value(None, "journal_mode", |row| row.get(0))
            .unwrap();
        assert_eq!(journal.to_ascii_lowercase(), "wal");
        let tables: i64 = connection.query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('files','chunks','dirty_queue','file_signals','glossary','cmd_audit')",
            [], |row| row.get(0),
        ).unwrap();
        assert_eq!(tables, 6);
    }

    #[test]
    fn dirty_queue_coalesces_events_and_acknowledges_only_matching_revision() {
        let (_dir, db) = database();
        let file = std::env::temp_dir().join("filebot-rust-dirty.txt");
        db.enqueue_dirty(&file, DirtyOperation::Upsert).unwrap();
        let first = db.pending_dirty(1).unwrap().remove(0);
        db.enqueue_dirty(&file, DirtyOperation::Delete).unwrap();
        let second = db.pending_dirty(1).unwrap().remove(0);
        assert_eq!(second.operation, DirtyOperation::Delete);
        assert!(second.queued_at > first.queued_at);
        assert!(!db.acknowledge_dirty(&file, first.queued_at).unwrap());
        assert!(db.acknowledge_dirty(&file, second.queued_at).unwrap());
    }

    #[test]
    fn commands_require_explicit_decision_and_are_claimed_once() {
        let (_dir, db) = database();
        let id = db
            .stage_command("whoami", ".", "show account", "read_only")
            .unwrap();
        assert!(db.claim_approved_command(&id).unwrap().is_none());
        assert!(db.decide_command(&id, true).unwrap());
        assert!(db.claim_approved_command(&id).unwrap().is_some());
        assert!(db.claim_approved_command(&id).unwrap().is_none());
        assert!(db.finish_command(&id, 0, "user\r\n", "").unwrap());
        let row = db.command_record(&id).unwrap().unwrap();
        assert_eq!(row.status, "executed");
        assert_eq!(row.stdout_tail.as_deref(), Some("user\r\n"));
    }

    #[test]
    fn recovery_requires_reapproval_or_marks_partial_execution() {
        let (_dir, db) = database();
        let ready = db.stage_command("mkdir a", ".", "create", "gated").unwrap();
        let claimed = db.stage_command("mkdir b", ".", "create", "gated").unwrap();
        assert!(db.decide_command(&ready, true).unwrap());
        assert!(db.decide_command(&claimed, true).unwrap());
        db.claim_approved_command(&claimed).unwrap().unwrap();
        assert_eq!(db.recover_command_states().unwrap(), (1, 1));
        assert_eq!(
            db.command_record(&ready).unwrap().unwrap().status,
            "pending"
        );
        let partial = db.command_record(&claimed).unwrap().unwrap();
        assert_eq!(partial.status, "failed");
        assert_eq!(partial.exit_code, Some(125));
    }

    #[test]
    fn failure_backoff_is_bounded_and_stale_errors_are_ignored() {
        let (_dir, db) = database();
        let file = std::env::temp_dir().join("filebot-rust-retry.txt");
        db.enqueue_dirty(&file, DirtyOperation::Upsert).unwrap();
        let first = db.pending_dirty(1).unwrap().remove(0);
        assert!(
            db.record_dirty_error(&file, first.queued_at, "bad input")
                .unwrap()
        );
        let ready = db.pending_dirty(1).unwrap();
        assert!(ready.is_empty());
        db.enqueue_dirty(&file, DirtyOperation::Delete).unwrap();
        assert!(
            !db.record_dirty_error(&file, first.queued_at, "stale")
                .unwrap()
        );
        let current = db.pending_dirty(1).unwrap().remove(0);
        assert_eq!(current.operation, DirtyOperation::Delete);
        assert_eq!(current.attempts, 0);
    }

    #[test]
    fn migrates_legacy_dirty_queue_without_dropping_pending_work() {
        let dir = tempfile::tempdir().expect("temporary directory");
        let path = dir.path().join("old.sqlite3");
        let legacy = Connection::open(&path).unwrap();
        legacy.execute_batch(
            "CREATE TABLE dirty_queue(path TEXT PRIMARY KEY,event_type TEXT NOT NULL,queued_at REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT);",
        ).unwrap();
        legacy.execute(
            "INSERT INTO dirty_queue(path,event_type,queued_at,attempts,last_error) VALUES(?1,'upsert',1.0,2,'old error')",
            ["C:\\Users\\Test\\notes.txt"],
        ).unwrap();
        drop(legacy);

        let db = Database::new(&path);
        db.initialize().unwrap();
        let connection = db.connect().unwrap();
        let columns = connection
            .prepare("PRAGMA table_info(dirty_queue)")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(1))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert!(columns.iter().any(|column| column == "available_at"));
        drop(connection);
        let row = db.pending_dirty(10).unwrap().remove(0);
        assert_eq!(row.attempts, 2);
        assert_eq!(row.last_error.as_deref(), Some("old error"));
    }

    #[test]
    fn blocked_proposals_are_audited_but_never_enter_pending_queue() {
        let (_dir, db) = database();
        let id = db
            .stage_command("format C:", ".", "destructive", "blocked")
            .unwrap();
        assert!(db.pending_commands().unwrap().is_empty());
        assert_eq!(db.command_record(&id).unwrap().unwrap().status, "blocked");
        assert!(!db.decide_command(&id, true).unwrap());
        assert!(db.claim_approved_command(&id).unwrap().is_none());
    }
}
