# FileBot Rust migration

This crate is the beginning of the native Rust implementation. The current
supported desktop application remains the Python 0.6.x build until the Rust
application passes the parity gates below. Do not launch or advertise this core
crate as a replacement desktop app yet.

## Current Rust coverage

- Typed loading and validation for the existing `config/settings.yaml`.
- WAL SQLite initialization using the existing table names and compatible
  column layout, including migration of the older `dirty_queue` schema.
- Recursive native filesystem watcher with initial scan, debounce, batching,
  extension/root/excluded-directory checks, and directory-delete propagation.
- Durable, revision-aware filesystem queue operations and retry backoff.
- Command proposal audit transitions, single-use claims, and crash recovery.
- Default-deny policy parsing from the existing `config/cmd_policy.yaml`, a
  proposal-only tool, live policy rechecks, and a bounded Windows CMD runner.
- Deterministic command/search/open/chat intent routing.
- Bounded TXT/Markdown/PDF/DOCX parsing and an awake-only dirty-queue service with
  SHA-256 revision checks, configured-root enforcement, retry handling, and
  transactional SQLite chunk storage.
- SQLite FTS5 lexical ranking plus fastembed BGE-small ONNX vectors, stored as
  float32 blobs and ranked with cosine similarity and reciprocal rank fusion.
- ONNX and model weights remain unloaded until `HybridEngine::activate()`; an
  inactive engine still serves local FTS5 search. The model is cached locally
  after first use, and subsequent activation can run without network access.
- A local-only Ollama runtime with model preflight, bounded turn history,
  per-intent tool allowlists, Qwen text-call compatibility, five-call limits,
  explicit proposal-only CMD handling, configured idle unload, and cancellable
  streaming for ordinary chat turns.
- Indexed file opening validates canonical paths, configured roots, and current
  SQLite index membership. The agent has no shell execution or file-write tool.
- A low-cost lifecycle supervisor starts dormant, handles wake/streamed turns,
  and unloads model resources after the configured idle interval.
- Ollama context/generation limits and embedding batch size are configurable;
  defaults cap context at 4096 tokens, output at 256 tokens, and embedding
  batches at 16 chunks. A 2-minute keep-alive warms back-to-back turns, while
  lifecycle sleep explicitly unloads the model.
- The always-on watcher caps its event channel and debounce map; overflow events
  are persisted directly to SQLite rather than growing memory without bound.
- Unsafe Rust is forbidden in the core crate.

Build and run the Rust core tests from the repository root with Rust 1.92 or newer:

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
cargo test --manifest-path rust/Cargo.toml --all-targets --locked
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
```

## Migration sequence

1. Add in-place migration tests against a copy of a real 0.6.x database. Keep
   the existing database usable throughout the port.
2. Add source-code parsing and test real 0.6.x database migration in place.
3. Benchmark hybrid retrieval and test semantic model cache/install behavior on
   clean Windows machines. Keep model construction behind an explicit wake
   boundary; SQLite stores vectors and FTS5 lexical ranking.
4. Verify chat streaming/cancellation and live Ollama model checks on Windows.
5. Build the avatar and popover as native Windows windows with `RegisterHotKey`,
   corner snapping, accessible controls, and explicit command approval.
6. Wire the native UI to the chat, watcher, and wake/sleep manager. Measure
   dormant and awake memory on Windows before calling the port an optimization.
7. Make the Rust app the default launch target only after CI, live Ollama,
   Windows UI, DB compatibility, and end-to-end command-gating checks pass.

Each stage must carry forward the Python implementation's path containment,
parser bounds, retry semantics, per-intent tool scoping, audit behavior, and
local-only inference. Rust is not considered a successful migration merely
because the code compiles or a benchmark microcase is faster.
