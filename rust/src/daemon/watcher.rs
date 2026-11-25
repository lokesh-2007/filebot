//! Low-memory filesystem watcher that persists changes without loading retrieval.
use crate::{
    config::{ConfigError, Settings, expand_path},
    storage::{Database, DirtyOperation, StorageError},
};
use notify::{Config as NotifyConfig, Event, RecommendedWatcher, RecursiveMode, Watcher};
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Component, Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, SyncSender, TrySendError},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use thiserror::Error;

const MAX_QUEUE_BATCH: usize = 500;
const MAX_WATCH_EVENTS: usize = 2048;
const MAX_PENDING_PATHS: usize = 4096;
const EVENT_POLL: Duration = Duration::from_millis(100);

#[derive(Debug, Error)]
pub enum WatchError {
    #[error("invalid watcher configuration: {0}")]
    Config(#[from] ConfigError),
    #[error("filesystem watcher error: {0}")]
    Notify(#[from] notify::Error),
    #[error("filesystem operation failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("database operation failed: {0}")]
    Storage(#[from] StorageError),
    #[error("watcher worker stopped unexpectedly")]
    WorkerStopped,
}

#[derive(Clone)]
struct WatchConfig {
    roots: Vec<PathBuf>,
    extensions: HashSet<String>,
    excluded_dirs: HashSet<String>,
    debounce: Duration,
}

struct PendingPath {
    directory: bool,
    deadline: Instant,
}

/// Owns the OS watcher and bounded worker threads for FileBot's sleeping state.
pub struct WatchService {
    watcher: Option<RecommendedWatcher>,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    seeder: Option<JoinHandle<()>>,
}

impl WatchService {
    /// Install recursive watches and seed existing files using only filesystem and SQLite work.
    pub fn start(settings: &Settings, db: Database) -> Result<Self, WatchError> {
        let index = &settings.index;
        let mut roots = Vec::new();
        for configured in &index.roots {
            let path = expand_path(configured)?;
            if path.is_dir() {
                let canonical = path.canonicalize()?;
                if !roots.contains(&canonical) {
                    roots.push(canonical);
                }
            }
        }
        let config = WatchConfig {
            roots: roots.clone(),
            extensions: index
                .include_extensions
                .iter()
                .map(|ext| ext.to_ascii_lowercase())
                .collect(),
            excluded_dirs: index
                .exclude_dirs
                .iter()
                .map(|name| name.to_ascii_lowercase())
                .collect(),
            debounce: Duration::from_secs_f64(index.debounce_seconds),
        };

        let (event_tx, event_rx) = mpsc::sync_channel::<notify::Result<Event>>(MAX_WATCH_EVENTS);
        let overflow_db = db.clone();
        let overflow_config = config.clone();
        let mut watcher = RecommendedWatcher::new(
            move |event| {
                persist_when_full(event, &event_tx, &overflow_db, &overflow_config);
            },
            NotifyConfig::default(),
        )?;
        for root in &roots {
            watcher.watch(root, RecursiveMode::Recursive)?;
        }

        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = Arc::clone(&stop);
        let worker_config = config.clone();
        let worker_db = db.clone();
        let worker = thread::Builder::new()
            .name("FileBotRustWatcher".into())
            .spawn(move || run_event_loop(event_rx, worker_db, worker_config, worker_stop))?;

        let seed_stop = Arc::clone(&stop);
        let seed_config = config;
        let seeder = thread::Builder::new()
            .name("FileBotRustInitialScan".into())
            .spawn(move || {
                for root in &seed_config.roots {
                    if seed_stop.load(Ordering::Acquire) {
                        break;
                    }
                    if let Err(error) = scan_tree(root, &seed_config, &db, &seed_stop) {
                        eprintln!(
                            "FileBot initial scan failed for {}: {error}",
                            root.display()
                        );
                    }
                }
            })?;

        Ok(Self {
            watcher: Some(watcher),
            stop,
            worker: Some(worker),
            seeder: Some(seeder),
        })
    }

    /// Stop producers, flush debounced file events, and join the lightweight workers.
    pub fn stop(&mut self) -> Result<(), WatchError> {
        self.watcher.take();
        self.stop.store(true, Ordering::Release);
        if let Some(seeder) = self.seeder.take() {
            seeder.join().map_err(|_| WatchError::WorkerStopped)?;
        }
        if let Some(worker) = self.worker.take() {
            worker.join().map_err(|_| WatchError::WorkerStopped)?;
        }
        Ok(())
    }
}

impl Drop for WatchService {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn run_event_loop(
    events: mpsc::Receiver<notify::Result<Event>>,
    db: Database,
    config: WatchConfig,
    stop: Arc<AtomicBool>,
) {
    let mut pending = HashMap::<PathBuf, PendingPath>::new();
    loop {
        if stop.load(Ordering::Acquire) {
            while let Ok(event) = events.try_recv() {
                handle_event_result(event, &mut pending, &db, &config);
            }
            flush_paths(&mut pending, &db, &config, true);
            return;
        }
        match events.recv_timeout(EVENT_POLL) {
            Ok(event) => handle_event_result(event, &mut pending, &db, &config),
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                flush_paths(&mut pending, &db, &config, true);
                return;
            }
        }
        flush_paths(&mut pending, &db, &config, false);
    }
}

fn handle_event_result(
    event: notify::Result<Event>,
    pending: &mut HashMap<PathBuf, PendingPath>,
    db: &Database,
    config: &WatchConfig,
) {
    match event {
        Ok(event) => queue_event(event, pending, db, config),
        Err(error) => eprintln!("FileBot filesystem notification error: {error}"),
    }
}

/// Keep the watcher callback channel bounded; persist overflow directly to SQLite.
fn persist_when_full(
    event: notify::Result<Event>,
    sender: &SyncSender<notify::Result<Event>>,
    db: &Database,
    config: &WatchConfig,
) {
    if let Err(TrySendError::Full(event)) = sender.try_send(event) {
        let mut pending = HashMap::with_capacity(8);
        handle_event_result(event, &mut pending, db, config);
        flush_paths(&mut pending, db, config, true);
    }
}

fn queue_event(
    event: Event,
    pending: &mut HashMap<PathBuf, PendingPath>,
    db: &Database,
    config: &WatchConfig,
) {
    let deleted = matches!(event.kind, notify::EventKind::Remove(_));
    let operation = if deleted {
        DirtyOperation::Delete
    } else {
        DirtyOperation::Upsert
    };
    for event_path in event.paths {
        let path = match canonical_event_path(&event_path) {
            Ok(path) => path,
            Err(_) => continue,
        };
        if !path_is_allowed(&path, operation, config) {
            continue;
        }
        let directory = path.is_dir()
            || (deleted
                && db
                    .paths_under(&path)
                    .map(|paths| !paths.is_empty())
                    .unwrap_or(false));
        if !directory && !has_supported_extension(&path, config) {
            continue;
        }
        if !pending.contains_key(&path) && pending.len() >= MAX_PENDING_PATHS {
            flush_paths(pending, db, config, true);
        }
        pending.insert(
            path,
            PendingPath {
                directory,
                deadline: Instant::now() + config.debounce,
            },
        );
    }
}

fn flush_paths(
    pending: &mut HashMap<PathBuf, PendingPath>,
    db: &Database,
    config: &WatchConfig,
    all: bool,
) {
    let now = Instant::now();
    let due = pending
        .iter()
        .filter_map(|(path, event)| (all || event.deadline <= now).then_some(path.clone()))
        .collect::<Vec<_>>();
    for path in due {
        if let Some(event) = pending.remove(&path) {
            let result = if event.directory {
                if path.is_dir() {
                    let never_stop = AtomicBool::new(false);
                    scan_tree(&path, config, db, &never_stop)
                } else {
                    db.paths_under(&path)
                        .map_err(WatchError::from)
                        .and_then(|paths| {
                            let events = paths
                                .into_iter()
                                .map(|item| (PathBuf::from(item), DirtyOperation::Delete))
                                .collect::<Vec<_>>();
                            db.enqueue_dirty_batch(&events).map_err(WatchError::from)
                        })
                }
            } else {
                let actual_operation = if path.exists() {
                    DirtyOperation::Upsert
                } else {
                    DirtyOperation::Delete
                };
                db.enqueue_dirty(&path, actual_operation)
                    .map_err(WatchError::from)
            };
            if let Err(error) = result {
                eprintln!(
                    "FileBot could not queue filesystem change {}: {error}",
                    path.display()
                );
            }
        }
    }
}

fn scan_tree(
    root: &Path,
    config: &WatchConfig,
    db: &Database,
    stop: &AtomicBool,
) -> Result<(), WatchError> {
    let mut directories = vec![root.to_path_buf()];
    let mut batch = Vec::<(PathBuf, DirtyOperation)>::with_capacity(MAX_QUEUE_BATCH);
    while let Some(directory) = directories.pop() {
        if stop.load(Ordering::Acquire) {
            break;
        }
        let entries = match fs::read_dir(&directory) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
        };
        for entry in entries {
            if stop.load(Ordering::Acquire) {
                break;
            }
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => continue,
                Err(error) => return Err(error.into()),
            };
            let path = entry.path();
            let metadata = match entry.file_type() {
                Ok(metadata) => metadata,
                Err(_) => continue,
            };
            if metadata.is_symlink() {
                continue;
            }
            if metadata.is_dir() {
                if !is_excluded(&path, config) {
                    directories.push(path);
                }
            } else if metadata.is_file() && has_supported_extension(&path, config) {
                batch.push((path, DirtyOperation::Upsert));
                if batch.len() >= MAX_QUEUE_BATCH {
                    db.enqueue_dirty_batch(&batch)?;
                    batch.clear();
                }
            }
        }
    }
    if !batch.is_empty() {
        db.enqueue_dirty_batch(&batch)?;
    }
    Ok(())
}

fn path_is_allowed(path: &Path, operation: DirtyOperation, config: &WatchConfig) -> bool {
    if !config.roots.iter().any(|root| path.starts_with(root)) || is_excluded(path, config) {
        return false;
    }
    operation == DirtyOperation::Delete || has_supported_extension(path, config) || path.is_dir()
}

fn has_supported_extension(path: &Path, config: &WatchConfig) -> bool {
    path.extension()
        .map(|extension| format!(".{}", extension.to_string_lossy()).to_ascii_lowercase())
        .is_some_and(|extension| config.extensions.contains(&extension))
}

fn is_excluded(path: &Path, config: &WatchConfig) -> bool {
    path.components()
        .filter_map(|component| match component {
            Component::Normal(name) => Some(name.to_string_lossy().to_ascii_lowercase()),
            _ => None,
        })
        .any(|name| config.excluded_dirs.contains(&name))
}

fn canonical_event_path(path: &Path) -> Result<PathBuf, std::io::Error> {
    if path.exists() {
        return path.canonicalize();
    }
    let parent = path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "event path has no parent directory",
        )
    })?;
    let name = path.file_name().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "event path has no file name",
        )
    })?;
    Ok(parent.canonicalize()?.join(name))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::DirtyOperation;

    fn config(root: &Path) -> WatchConfig {
        WatchConfig {
            roots: vec![root.canonicalize().unwrap()],
            extensions: [".txt".to_owned(), ".md".to_owned()].into(),
            excluded_dirs: [".git".to_owned(), "node_modules".to_owned()].into(),
            debounce: Duration::from_millis(10),
        }
    }

    #[test]
    fn watches_only_supported_files_within_roots_and_outside_excluded_dirs() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("docs");
        fs::create_dir_all(root.join(".git")).unwrap();
        fs::create_dir_all(root.join("private")).unwrap();
        let supported = root.join("notes.txt");
        let excluded = root.join(".git/secret.txt");
        let other = root.join("private/code.rs");
        fs::write(&supported, "ok").unwrap();
        fs::write(&excluded, "ignore").unwrap();
        fs::write(&other, "ignore").unwrap();
        let config = config(&root);
        assert!(path_is_allowed(
            &supported.canonicalize().unwrap(),
            DirtyOperation::Upsert,
            &config
        ));
        assert!(!path_is_allowed(
            &excluded.canonicalize().unwrap(),
            DirtyOperation::Upsert,
            &config
        ));
        assert!(!path_is_allowed(
            &other.canonicalize().unwrap(),
            DirtyOperation::Upsert,
            &config
        ));
        assert!(!path_is_allowed(
            &temp.path().join("outside.txt"),
            DirtyOperation::Upsert,
            &config
        ));
    }

    #[test]
    fn saturated_event_channel_persists_the_overflow_event_to_sqlite() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("docs");
        fs::create_dir_all(&root).unwrap();
        let queued = root.join("overflow.txt");
        fs::write(&queued, "durable event").unwrap();
        let config = config(&root);
        let db = Database::new(temp.path().join("filebot.db"));
        db.initialize().unwrap();
        let (sender, _receiver) = mpsc::sync_channel(1);
        sender
            .try_send(Ok(Event::new(notify::EventKind::Any)))
            .unwrap();
        let event = Event::new(notify::EventKind::Create(notify::event::CreateKind::File))
            .add_path(queued.clone());

        persist_when_full(Ok(event), &sender, &db, &config);

        let dirty = db.pending_dirty(10).unwrap();
        assert_eq!(dirty.len(), 1);
        assert_eq!(
            dirty[0].path.to_ascii_lowercase(),
            queued.to_string_lossy().to_ascii_lowercase()
        );
    }

    #[test]
    fn initial_scan_seeds_supported_files_in_bounded_database_batches() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("docs");
        fs::create_dir_all(root.join("node_modules")).unwrap();
        fs::write(root.join("one.md"), "one").unwrap();
        fs::write(root.join("node_modules/two.md"), "ignore").unwrap();
        fs::write(root.join("three.pdf"), "unsupported").unwrap();
        let db = Database::new(temp.path().join("state.sqlite3"));
        db.initialize().unwrap();
        scan_tree(&root, &config(&root), &db, &AtomicBool::new(false)).unwrap();
        let queued = db.pending_dirty(10).unwrap();
        assert_eq!(queued.len(), 1);
        assert!(queued[0].path.ends_with("one.md"));
    }

    #[test]
    fn deleting_a_directory_queues_all_indexed_and_dirty_descendants() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("docs");
        let removed = root.join("old");
        fs::create_dir_all(&removed).unwrap();
        let file = removed.join("notes.txt");
        let db = Database::new(temp.path().join("state.sqlite3"));
        db.initialize().unwrap();
        db.enqueue_dirty(&file, DirtyOperation::Upsert).unwrap();
        fs::remove_dir_all(&removed).unwrap();
        let config = config(&root);
        let paths = db.paths_under(&removed).unwrap();
        let batch = paths
            .into_iter()
            .map(|path| (PathBuf::from(path), DirtyOperation::Delete))
            .collect::<Vec<_>>();
        db.enqueue_dirty_batch(&batch).unwrap();
        let queued = db.pending_dirty(10).unwrap();
        assert_eq!(queued.len(), 1);
        assert_eq!(queued[0].operation, DirtyOperation::Delete);
        assert!(path_is_allowed(
            &canonical_event_path(&removed).unwrap(),
            DirtyOperation::Delete,
            &config
        ));
    }
}
