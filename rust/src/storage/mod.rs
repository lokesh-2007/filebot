//! Local SQLite persistence.

mod db;

pub use db::{
    ChunkInput, CommandRecord, Database, DirtyEvent, DirtyOperation, SearchHit, StorageError,
    UnembeddedChunk,
};
