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
- Bounded TXT/Markdown chunking and transactional file/chunk replacement,
  deletion, and parameterized SQLite lexical search.
- Unsafe Rust is forbidden in the core crate.

Build and run the Rust core tests from the repository root:

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
cargo test --manifest-path rust/Cargo.toml --all-targets
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
```

## Migration sequence

1. Add in-place migration tests against a copy of a real 0.6.x database. Keep
   the existing database usable throughout the port.
2. Port bounded TXT/Markdown, PDF, DOCX, and source-code parsing and indexing.
3. Port hybrid retrieval. Keep model construction behind an explicit wake
   boundary; use SQLite FTS5 plus a local vector store unless benchmarks show a
   better fully embedded Rust option.
4. Port Ollama chat and the narrow search/open/propose tools. Ordinary chat must
   not receive action tools, and model output must never invoke the shell.
5. Build the avatar and popover as native Windows windows with `RegisterHotKey`,
   corner snapping, accessible controls, and explicit command approval.
6. Integrate the watcher and wake/sleep manager. Measure dormant and awake memory on
   Windows before calling the port an optimization.
7. Make the Rust app the default launch target only after CI, live Ollama,
   Windows UI, DB compatibility, and end-to-end command-gating checks pass.

Each stage must carry forward the Python implementation's path containment,
parser bounds, retry semantics, per-intent tool scoping, audit behavior, and
local-only inference. Rust is not considered a successful migration merely
because the code compiles or a benchmark microcase is faster.
