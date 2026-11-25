//! Durable dirty-queue processor for the sleeping watcher and awake parser.
use crate::{
    config::{ConfigError, Settings, expand_path},
    retrieval::parsers::{ParseOptions, parse_file},
    storage::{ChunkInput, Database, DirtyOperation, StorageError},
};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs::File,
    io::{self, Read},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, UNIX_EPOCH},
};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum IndexError {
    #[error("invalid index configuration: {0}")]
    Config(#[from] ConfigError),
    #[error("database operation failed: {0}")]
    Storage(#[from] StorageError),
    #[error("filesystem operation failed: {0}")]
    Io(#[from] io::Error),
    #[error("document parsing failed: {0}")]
    Parser(#[from] super::parsers::ParseError),
}

/// Parses a bounded batch of filesystem events and commits chunks to SQLite.
pub struct IndexWorker {
    db: Database,
    roots: Vec<PathBuf>,
    extensions: HashSet<String>,
    options: ParseOptions,
    max_file_bytes: u64,
}

impl IndexWorker {
    /// Construct a worker from validated app settings. Roots are canonicalized once.
    pub fn new(settings: &Settings, db: Database) -> Result<Self, IndexError> {
        let roots = settings
            .index
            .roots
            .iter()
            .filter_map(|root| expand_path(root).ok())
            .filter_map(|root| root.canonicalize().ok())
            .collect::<Vec<_>>();
        let extensions = settings
            .index
            .include_extensions
            .iter()
            .map(|ext| ext.trim_start_matches('.').to_ascii_lowercase())
            .collect();
        Ok(Self {
            db,
            roots,
            extensions,
            options: ParseOptions {
                chunk_chars: settings.index.chunk_chars,
                overlap_chars: settings.index.chunk_overlap,
                max_bytes: settings.index.max_file_bytes,
                max_chars: settings.index.max_extracted_chars,
                max_docx_uncompressed_bytes: settings.index.max_docx_uncompressed_bytes,
            },
            max_file_bytes: settings.index.max_file_bytes,
        })
    }

    /// Drain at most `limit` ready events. Errors are retained with durable retry backoff.
    pub fn drain(&self, limit: usize, stop: Option<&AtomicBool>) -> Result<usize, IndexError> {
        let events = self.db.pending_dirty(limit)?;
        let mut processed = 0;
        for event in events {
            if stop.is_some_and(|signal| signal.load(Ordering::Acquire)) {
                break;
            }
            let path = PathBuf::from(&event.path);
            let result = self.process_one(&path, event.operation);
            match result {
                Ok(()) => {
                    self.db.acknowledge_dirty(&path, event.queued_at)?;
                    processed += 1;
                }
                Err(error) => {
                    self.db
                        .record_dirty_error(&path, event.queued_at, &error.to_string())?;
                }
            }
        }
        Ok(processed)
    }

    fn process_one(&self, path: &Path, operation: DirtyOperation) -> Result<(), IndexError> {
        if operation == DirtyOperation::Delete || !path.exists() {
            self.db.delete_file(path)?;
            return Ok(());
        }
        let canonical = match path.canonicalize() {
            Ok(path) => path,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                self.db.delete_file(path)?;
                return Ok(());
            }
            Err(error) => return Err(error.into()),
        };
        if !self.roots.iter().any(|root| canonical.starts_with(root)) {
            return Ok(());
        }
        let extension = canonical
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if !self.extensions.contains(&extension) {
            self.db.delete_file(&canonical)?;
            return Ok(());
        }
        let before = canonical.metadata()?;
        if !before.is_file() {
            return Ok(());
        }
        if before.len() > self.max_file_bytes {
            self.db.delete_file(&canonical)?;
            return Ok(());
        }
        let mut hasher = Sha256::new();
        let mut file = File::open(&canonical)?;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            hasher.update(&buffer[..count]);
        }
        let after_hash = canonical.metadata()?;
        if !same_revision(&before, &after_hash) {
            self.db.enqueue_dirty(&canonical, DirtyOperation::Upsert)?;
            return Ok(());
        }
        let parsed = parse_file(&canonical, self.options)?;
        let after_parse = canonical.metadata()?;
        if !same_revision(&after_hash, &after_parse) {
            self.db.enqueue_dirty(&canonical, DirtyOperation::Upsert)?;
            return Ok(());
        }
        let modified = after_parse
            .modified()?
            .duration_since(UNIX_EPOCH)
            .map_err(|_| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "file modification time predates Unix epoch",
                )
            })?
            .as_secs_f64();
        let chunks = parsed
            .into_iter()
            .map(|chunk| ChunkInput {
                text: chunk.text,
                page_or_line: chunk.page_or_line,
                heading: chunk.heading,
            })
            .collect::<Vec<_>>();
        self.db.replace_file(
            &canonical,
            &format!("{:x}", hasher.finalize()),
            after_parse.len(),
            modified,
            &chunks,
        )?;
        Ok(())
    }
}

fn same_revision(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    left.len() == right.len() && left.modified().ok() == right.modified().ok()
}

/// Background queue service. Start it only when retrieval is awake; stop to release the worker.
pub struct IndexQueueService {
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl IndexQueueService {
    /// Spawn one bounded polling worker.
    pub fn start(
        indexer: Arc<IndexWorker>,
        batch_size: usize,
        poll: Duration,
    ) -> Result<Self, IndexError> {
        if batch_size == 0 || poll.is_zero() {
            return Err(IndexError::Config(ConfigError::Invalid(
                "index queue batch and poll must be positive".into(),
            )));
        }
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        let worker = thread::Builder::new()
            .name("FileBotRustIndexQueue".into())
            .spawn(move || {
                while !thread_stop.load(Ordering::Acquire) {
                    let processed = indexer
                        .drain(batch_size, Some(&thread_stop))
                        .unwrap_or_else(|error| {
                            eprintln!("FileBot index queue error: {error}");
                            0
                        });
                    if processed == 0 {
                        thread::park_timeout(poll);
                    }
                }
            })?;
        Ok(Self {
            stop,
            worker: Some(worker),
        })
    }

    /// Stop and join the indexing thread before releasing retrieval resources.
    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            worker.thread().unpark();
            let _ = worker.join();
        }
    }
}

impl Drop for IndexQueueService {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::DirtyOperation;
    use tempfile::tempdir;

    #[test]
    fn drains_markdown_and_delete_events_with_root_containment() {
        let directory = tempdir().unwrap();
        let root = directory.path().join("docs");
        std::fs::create_dir_all(&root).unwrap();
        let file = root.join("guide.md");
        std::fs::write(&file, "# Local\nNeedle is searchable.").unwrap();
        let mut settings = crate::config::load_settings(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../config/settings.yaml"
        ))
        .unwrap();
        settings.index.roots = vec![root.to_string_lossy().into_owned()];
        settings.index.include_extensions = vec![".md".into()];
        let db = Database::new(directory.path().join("filebot.db"));
        db.initialize().unwrap();
        let worker = IndexWorker::new(&settings, db.clone()).unwrap();
        db.enqueue_dirty(&file, DirtyOperation::Upsert).unwrap();
        assert_eq!(worker.drain(10, None).unwrap(), 1);
        assert!(
            db.pending_dirty(10).unwrap().is_empty(),
            "upsert queue should be acknowledged"
        );
        let hit = db.search_lexical("Needle", 5).unwrap();
        assert_eq!(hit.len(), 1);
        assert_eq!(hit[0].heading.as_deref(), Some("Local"));
        db.enqueue_dirty(&file, DirtyOperation::Delete).unwrap();
        let queued = db.pending_dirty(10).unwrap();
        assert_eq!(queued.len(), 1);
        assert!(queued[0].path.eq_ignore_ascii_case(&hit[0].path));
        assert_eq!(worker.drain(10, None).unwrap(), 1);
        assert!(db.search_lexical("Needle", 5).unwrap().is_empty());
    }
}
