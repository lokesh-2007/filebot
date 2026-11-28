# FileBot Rust migration

The Rust migration includes the core library and an early native Win32 desktop
preview. The supported desktop release remains Python 0.6.x until the Rust
application passes the Windows UI and performance gates below.

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
- Configured glossary aliases seed SQLite once, user entries can be replaced
  transactionally, and Rust search expands aliases at word boundaries.
- Successful opens and helpful/unhelpful votes persist in the shared
  `file_signals` table and apply a bounded ranking adjustment; the native UI
  exposes votes for its selected result.
- ONNX and model weights remain unloaded until `HybridEngine::activate()`; an
  inactive engine still serves local FTS5 search. The model is cached locally
  after first use, and subsequent activation can run without network access.
- A local-only Ollama runtime with model preflight, bounded turn history,
  per-intent tool allowlists, Qwen text-call compatibility, five-call limits,
  explicit proposal-only CMD handling, configured idle unload, and cancellable
  streaming for ordinary chat turns. If configured, it starts the local Ollama
  server on the first model request, waits for readiness, and retries startup if
  its child server process exits. Stop cancels between 25 ms startup-poll waits
  and terminates only the child process FileBot started; an in-flight HTTP
  readiness probe is bounded to two seconds before Stop is observed.
- Indexed file opening validates canonical paths, configured roots, and current
  SQLite index membership. The agent has no shell execution or file-write tool.
- A low-cost lifecycle supervisor starts dormant, handles wake/streamed turns,
  and unloads model resources after the configured idle interval.
- Ollama context/generation limits and embedding batch size are configurable;
  defaults cap context at 4096 tokens, output at 256 tokens, and embedding
  batches at 16 chunks. Validation follows the Python-supported bounds: context
  at most 4096, output no larger than context, embedding batches at most 16,
  and background indexing batches at most 10. Omitted embedding batch size
  defaults to eight. A 2-minute keep-alive warms back-to-back turns, while
  lifecycle sleep explicitly unloads the model.
- Turn diagnostics expose preflight time, time-to-first-token, total latency,
  and response length without recording prompt or response text.
- The always-on watcher caps its event channel and debounce map; overflow events
  are persisted directly to SQLite rather than growing memory without bound.
- Unsafe Rust is forbidden in the core crate.
- A separate `rust/desktop` crate provides a compact Win32 avatar and popover,
  configurable global hotkey, streamed chat with cancellation, local lexical
  search, visible pending-command approval controls, and a bounded UI event
  queue/transcript. Search and indexing run off the UI thread; the status row
  displays lifecycle state and process working-set RAM. Its UI uses no browser
  or Qt runtime.

Build and run the Rust core tests from the repository root with Rust 1.92 or newer:

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
cargo test --manifest-path rust/Cargo.toml --all-targets --locked
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
```

The core and desktop crates can be type-checked offline with
`ORT_SKIP_DOWNLOAD=1` when their Cargo dependencies are cached. Running the
linked core test executable still requires a compatible ONNX Runtime library;
without it, tests fail at the Windows linker with unresolved
`OrtGetApiBase` symbols. A successful `cargo check` is not a substitute for
executing those tests.

Check the native Windows UI crate (the ONNX runtime binary download is not
needed for type checking):

```powershell
$env:ORT_SKIP_DOWNLOAD = "1"
cargo check --manifest-path rust/desktop/Cargo.toml --locked
cargo clippy --manifest-path rust/desktop/Cargo.toml --all-targets --locked -- -D warnings
```

To run the desktop preview, build on Windows with network access so the ONNX
Runtime binary can be installed, then launch `filebot.exe` beside the `config`
directory. First activation downloads the configured embedding model if it is
not already cached; chat requires the configured local Ollama model.

## Migration sequence

1. Add in-place migration tests against a copy of a real 0.6.x database. Keep
   the existing database usable throughout the port.
2. Add source-code parsing and test real 0.6.x database migration in place.
3. Benchmark hybrid retrieval and test semantic model cache/install behavior on
   clean Windows machines. Keep model construction behind an explicit wake
   boundary; SQLite stores vectors and FTS5 lexical ranking.
4. Verify chat streaming/cancellation and live Ollama model checks on Windows.
5. Complete native UI parity: keyboard accessibility, clickable file paths,
   multi-monitor corner snapping, status polish, and an installer-safe config
   location. The first Win32 shell preview is in place.
6. Measure UI startup, wake-to-first-result latency, dormant working set, and
   model-load/unload memory on representative Windows machines; tune from those
   measurements before calling the port an optimization.
7. Make the Rust app the default launch target only after CI, live Ollama,
   Windows UI, DB compatibility, and end-to-end command-gating checks pass.

Each stage must carry forward the Python implementation's path containment,
parser bounds, retry semantics, per-intent tool scoping, audit behavior, and
local-only inference. Rust is not considered a successful migration merely
because the code compiles or a benchmark microcase is faster.
