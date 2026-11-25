//! Rust agent routing and narrow tool set.

mod fast_router;
pub mod ollama_runtime;
pub mod tools;

pub use fast_router::{FastRouter, Intent};
pub use ollama_runtime::TurnMetrics;
