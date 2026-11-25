//! Local SQLite persistence.

mod db;

pub use db::{CommandRecord, Database, DirtyEvent, DirtyOperation, StorageError};
