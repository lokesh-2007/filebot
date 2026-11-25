//! Lightweight always-on services.

mod lifecycle_manager;
mod watcher;

pub use lifecycle_manager::{LifecycleManager, LifecycleState, ManagedAssistant};
pub use watcher::{WatchError, WatchService};
