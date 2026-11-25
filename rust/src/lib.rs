//! Rust implementation of FileBot's core persistence, configuration, and safety policy.
//!
//! The desktop executable and retrieval runtime are being migrated in separate
//! feature-complete stages. This crate deliberately keeps heavyweight inference
//! and embedding dependencies out of its baseline build.

pub mod config;
pub mod executor;
pub mod storage;

/// FileBot Rust core version, independent from the currently shipping Python app.
pub const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");
