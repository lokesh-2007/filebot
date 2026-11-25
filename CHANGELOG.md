# Changelog

## Responsive local assistant improvements 0.6.7 — 2026-11-25

- Add an in-app glossary editor with add/remove controls and transactional
  SQLite replacement; user edits remain authoritative across restarts.
- Seed configured aliases once, so deleted aliases are not restored at wake.
- Correct Tree-sitter integration to use the pinned package's offline registry
  check and bounded processing API; fix the C# grammar's canonical name.
- Add a Stop control that cancels an active streamed/model request without
  blocking the popover; hide it for local-only search turns.
- Log request duration and time-to-first-token in the rotating local log without
  recording prompts, snippets, or model output.
- Log wake initialization time and Windows working-set snapshots after dormant
  startup, wake, and idle unload to support on-device tuning.
- Verify glossary persistence, source chunk metadata, offline parser fallback,
  and background indexing priority with focused tests.

## Source indexing and queue priority 0.6.6 — 2026-11-25

- Add bounded Tree-sitter syntax-aware chunks for common source/config languages,
  including line ranges and symbol headings. Cache grammars during setup; at
  runtime, check local parser availability first and never download on wake.
- Add the configured code extensions and parser deadline, with bounded text
  fallback when a selected grammar is not cached or parsing fails.
- Run the indexing worker in Windows background CPU and I/O priority mode and
  restore normal priority when the worker exits.
- Verify parser bounds, offline fallback, background-priority cleanup, and the
  existing watcher/index retry behavior with focused tests.

## Faster wake feedback 0.6.5 — 2026-11-25

- Show the popover immediately while retrieval resources initialize in the
  background, then attach search/chat services to the same card when ready.
- Gate requests during startup with a clear loading message and dismiss the
  loading card cleanly on cancel or initialization failure.
- Verify the behavior with the full Qt lifecycle test suite.

## Responsiveness and on-demand chat 0.10.2 / 0.6.4 — 2026-11-25

- Route obvious file searches in Ask directly to the local index. This avoids
  Ollama startup and agent overhead for a request the index can already answer.
- Open explicitly named indexed files directly through the safe root/index
  checks, avoid the model for that common action, and show choices for duplicate
  basenames. Persist successful opens and Helpful/Not helpful feedback as bounded
  retrieval ranking signals.
- Expand configured personal shorthand through SQLite's glossary table before
  retrieval while preserving exact-token matching and hybrid relevance.
- Keep queue draining in the background index worker instead of synchronously
  processing up to 100 files before returning search results.
- Start the Windows Ollama server on the first model-backed request when enabled
  in settings, then report a bounded startup timeout or a missing model clearly.
- Fix frameless avatar clicks and add a root-checked open action for Rust search
  results. Search remains available without loading the language model.
- Focused Python runtime/config checks pass; Rust formatting, check, and Clippy
  pass. Full local Python discovery remains subject to the managed Windows
  loopback socket restriction; CI runs the supported Python 3.11/3.12 matrix.

## Rust desktop responsiveness 0.10.1 — 2026-11-25

- Run local SQLite search on a background thread so index size does not block
  avatar and popover input.
- Display the current lifecycle state and Windows working-set memory in the
  popover status row; keep per-turn latency metrics visible after streaming.
- Start the bounded dirty-queue index worker so changed files become searchable
  without activating the ONNX embedding model.
- Verified with offline Rust core and desktop type-checks, formatting, and
  warning-free Clippy. Windows CI verifies tests and the native desktop crate.

## Rust migration desktop preview 0.10.0 — 2026-11-25

- Add a separate, low-overhead Win32 desktop shell with a draggable corner
  avatar, configurable hotkey, popover search/chat UI, streamed output,
  cancellation, and explicit command approval/rejection controls. Start the
  bounded SQLite dirty-queue index worker so newly watched files become
  searchable without loading the embedding model.
- Keep chat/runtime calls off the UI thread, cap pending UI events and visible
  transcript memory, run SQLite search away from the window thread, and poll
  command state at a bounded interval.
- Show first-token, Ollama preflight, and total turn timings in the popover to
  make latency tuning observable without logging prompt or response text.
- Add a Windows CI type-check and clippy gate for the desktop crate. Offline
  `cargo check` and warning-free clippy pass; live Windows UI and working-set
  measurements remain outstanding.

Version numbers follow semantic versioning. This project is pre-1.0; minor
versions may still change behavior while the Windows release is stabilized.

## Rust migration core 0.1.0 — 2026-11-25

- Add a separately tested Rust core for typed settings, WAL SQLite persistence,
  durable dirty-queue retries, command audit transitions, and the default-deny
  CMD policy. The crate is not yet the desktop app; Python 0.6.3 remains the
  supported Windows executable until Rust reaches feature parity.
- Add Windows CI formatting, clippy, and Rust unit-test gates. The 15 core tests,
  formatting check, and warning-free clippy run pass locally.

## Rust migration core 0.2.0 — 2026-11-25

- Port the low-memory recursive watcher, initial scan, root/extension/exclusion
  checks, debounce, and directory rename/delete queueing to Rust.
- Add the proposal-only CMD tool, deterministic intent router, and approved
  command runner with live policy rechecks, a sanitized System32 search path,
  bounded output, timeout, and durable audit completion.
- Add bounded TXT/Markdown parsing and transactional SQLite file/chunk
  replacement, deletion, and lexical search.
- Verified with 29 Rust tests, formatting, and warning-free clippy.

## Rust migration core 0.3.0 — 2026-11-25

- Add native PDF and DOCX extraction with page/heading metadata and bounded
  output/ZIP expansion, plus a durable index queue worker with SHA-256 revisions,
  configured-root checks, retry backoff, and worker shutdown.
- Replace substring matching with maintained FTS5 lexical ranking; existing
  SQLite chunks are backfilled into the FTS index on initialization.
- Normalize Windows extended-length paths and match file operations
  case-insensitively so watcher events and canonical paths refer to one record.
- Verified with 34 Rust tests, formatting, and warning-free clippy.

## Rust migration core 0.4.0 — 2026-11-25

- Add a dormant fastembed ONNX engine using BGE-small, persisted float32 vectors,
  bounded incremental embedding, cosine search, and RRF fusion with SQLite FTS5.
- ONNX and model weights initialize only after explicit activation; the engine
  keeps FTS5 search available when semantic inference is inactive or unavailable.
- Verify Rust 1.92 MSRV and lockfile usage in Windows CI. Verified with 37 Rust
  tests, formatting, and warning-free clippy.

## Rust migration core 0.5.0 — 2026-11-25

- Add a lazy local Ollama chat runtime with bounded conversation history,
  configured keep-alive unload, and model preflight checks.
- Restrict each routed turn to one tool surface: search, indexed/root-contained
  file open, or proposal-only CMD. No shell tool is exposed to model output.
- Support Qwen text-encoded function calls, cap tool turns/calls, bound HTTP
  response bodies, and reject non-loopback Ollama endpoints.
- Verified with 43 Rust tests, locked dependencies, formatting, and warning-free
  clippy. The native UI and lifecycle wiring remain in progress.

## Rust migration core 0.6.0 — 2026-11-25

- Stream ordinary local chat text as Ollama emits it and let the UI cancel the
  active read. A cancelled answer is not appended to conversation history.
- Keep action-intent turns buffered so model-generated tool-call sketches are
  validated before an action result is presented as assistant prose.
- Preserve the 2 MiB response cap and report stream read failures explicitly.
- Verified with 45 Rust tests, locked dependencies, formatting, and warning-free
  clippy. Live Ollama and Windows UI checks remain outstanding.

## Rust migration core 0.7.0 — 2026-11-25

- Add a dormant lifecycle supervisor with explicit wake/sleep, streamed turns,
  automatic idle unload, and avatar-ready `sleep`/`active` states.
- Stop its lightweight worker cleanly on drop and surface worker startup errors
  instead of panicking. Supervisor startup does not load Ollama or ONNX models.
- Verified with 47 Rust tests, locked dependencies, formatting, and warning-free
  clippy. Native UI wiring and live Ollama checks remain outstanding.

## Rust migration core 0.8.0 — 2026-11-25

- Bound the always-on watcher channel to 2048 events and its debounce map to
  4096 paths. Saturation persists events through the SQLite dirty queue instead
  of allowing an unbounded in-memory backlog.
- Add configurable Ollama context and response-token limits (4096/256 defaults)
  and reduce embedding batches from 64 to 16 for lower transient RAM use.
- Keep the model warm for 2 minutes between active turns by default, then send
  an immediate unload request at lifecycle sleep to avoid retaining model RAM.
- Verified with 50 Rust tests, locked dependencies, formatting, and warning-free
  clippy. Measure real Windows latency/RAM and resolve the CI check before
  calling these defaults validated.

## Rust migration core 0.8.1 — 2026-11-25

- Canonicalize existing files and parent directories before forming SQLite path
  keys, resolving Windows short-name aliases so indexing, updates, and deletes
  address one record.
- Correct Windows path assertions and retain a saturation-path regression test.
- Verified with 50 Rust tests, formatting, and warning-free clippy on Windows.

## Rust migration core 0.9.0 — 2026-11-25

- Expose per-turn preflight, first-token, total elapsed time, and response-size
  metrics to the future UI without retaining or logging prompt text.
- Resolve Windows short-name aliases even for missing nested paths by
  canonicalizing the nearest existing ancestor before building the SQLite key.
- Keep cancellation and timing instrumentation in the same streaming path so
  response latency can be measured from real use rather than guessed.
- Verified with 50 Rust tests, formatting, and warning-free clippy. Product UI
  integration and on-device latency/RAM benchmarks remain next.

## 0.6.3 — 2026-11-25

- Recheck the local Ollama endpoint and configured model before every chat turn,
  so a stopped service or removed model during an active session gets clear
  recovery guidance instead of a raw connection failure.
- Add regression coverage for reused chat sessions.

## 0.6.2 — 2026-11-25

- Pin the verified Nanobot SDK and Ollama provider versions so upstream API
  changes cannot silently break chat or streamed tool calls on fresh installs.

## 0.6.1 — 2026-11-25

- Check the configured local Ollama endpoint and model before loading the agent.
  Show a direct recovery step when Ollama is stopped or the model has not been
  pulled.
- Document the chat setup recovery steps and add preflight regression coverage.
- Confirmed the preflight reaches the local Ollama server and finds the configured
  `qwen2.5-coder:3b` model on this machine.

## 0.6.0 — 2026-11-25

- Extend the 0.5 production hardening with bounded document parsing, path-root
  enforcement, durable indexing retries, and file-revision consistency checks.
- Stream model text into the popover and scope agent tools to the detected
  intent. Ordinary chat receives no tools; search, open, and command requests
  receive only their matching local tool.
- Add the architecture and safety guide in `AGENT.md`.
- Verified with 64 unit tests plus compile and dependency checks. A live local
  chat returned `CHAT_STREAM_OK`, streamed its first text after about 33 seconds,
  and did not make an unnecessary file search call. A CMD model turn staged
  `whoami` as pending without entering the command runner.

## 0.5.0 — 2026-11-25

- Added lazy model loading, bounded embedding memory, command output limits,
  single-instance guarding, database resource cleanup, and Windows setup scripts.
- Added Windows CI coverage for production dependencies and Python 3.11/3.12.
- This remains a source release without a signed installer.

## 0.1.4 — 2026-11-25

- Stream local model text into the popover as it arrives.
- Limit each agent turn to tools appropriate for its intent: no tools for
  ordinary chat, file search for search requests, file opening for open
  requests, and command proposals for CMD requests.
- A live local-model chat returned `CHAT_STREAM_OK` and began streaming text
  after about 33 seconds on this machine, without making an unnecessary file
  search call.
- A live CMD request staged `whoami` as a pending read-only proposal; the model
  path did not invoke the command runner. Verified with 64 unit tests plus
  compile, dependency, and diff checks.

## 0.1.3 — 2026-11-25

- Make search results explain how to open a match or ask the local model about
  the indexed files.
- Keep the chat panel visibly updated with elapsed wait time while Ollama
  loads or generates a response.
- Verified the real Nanobot/Ollama chat path with the configured local model;
  a cold first response completed in about 34 seconds. All 61 unit tests pass,
  along with compile, dependency, and diff checks.

## 0.1.2 — 2026-11-25

- Keep each indexed file's content hash, metadata, and parsed chunks tied to the
  same filesystem revision. If a file changes during indexing, leave it queued
  for a fresh pass instead of storing mismatched search content.
- Added a regression test that changes a file while parsing and verifies that
  only the new revision is indexed on the next pass.
- Verified with 60 unit tests plus compile, dependency, and diff checks.

## 0.1.1 — 2026-11-25

- Bound extracted text and DOCX archive expansion to protect memory during
  indexing of large or highly-compressed files.
- Hash files in 1 MB blocks instead of reading each complete file into memory.
- Verified with 59 unit tests plus compile, dependency, and workflow checks.

## 0.1.0 — 2026-11-25

- Delivered the Windows avatar, hotkey, popover, local indexing, hybrid search,
  Ollama chat, and approval-gated CMD workflow.
- Added root-scoped indexing and file opening, exact short-token search, and a
  Nanobot streaming tool-call adapter for Qwen's text-form tool responses.
- Added SQLite retry backoff and legacy queue migration, plus safe recovery of
  command audit rows after an unclean shutdown.
- Added Windows setup and launch scripts, privacy/operations documentation, and
  CI coverage for Python 3.11 and 3.12.
- Verified the current tree with 57 unit tests, compile checks, dependency checks,
  and a live local Ollama tool-call check.

This version is a source release. It does not include a signed installer.
