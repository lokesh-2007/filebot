# FileBot product roadmap

The attached **FileBot Local File & CMD Assistant Architecture & Research**
document is the product acceptance blueprint. Its recommendations are treated
as project requirements; a reference document does not override explicit user
instructions. Python remains the supported desktop app while the Rust port is
still being validated.

## Implemented in the supported Python app

- One-prompt resizable chat window, system tray, global hotkey, watchdog watcher,
  and SQLite WAL queue run without importing the embedding or agent runtime at
  startup.
- Wake shows a loading popover immediately while retrieval initialization runs
  off the GUI thread; controls activate when the same card becomes ready.
- An in-flight local model request can be cancelled from the popover without
  blocking Qt's UI thread; cancellation covers Ollama preflight/startup and a
  configured deadline bounds a stalled model turn.
- Successful local Ollama/model readiness checks are cached for 90 seconds, so
  quick follow-up turns skip the `/api/tags` HTTP probe; inference errors
  invalidate the cache. First-token wait feedback appears every three seconds,
  and model prompts ask for natural, concise, result-first replies. This reduces
  preflight and perceived wait overhead; no new hardware inference benchmark was
  run, so token-generation latency is not claimed to have improved.
- Retrieval and Nanobot initialize on wake; Ollama starts on demand for model
  requests and unloads according to the configured keep-alive/lifecycle policy.
- FileBot-managed Ollama starts with one loaded model, one parallel request,
  and the configured context cap; embedding and background indexing batches are
  kept small to reduce transient memory and retrieval lock time. The in-memory
  BM25 corpus is capped at 10,000 chunks; larger corpora retain vector search
  and use SQLite lexical fallback. The 3.5–4 GB product target is not yet a
  measured or OS-enforced hard limit, especially when Ollama is already running
  outside FileBot.
- Align Rust model and indexing validation with Python's supported bounded
  profile: context at most 4096, generation no greater than context, embedding
  batches at most 16, and background indexing batches at most 10. When an older
  settings file omits embedding batch size, both implementations now use the
  bounded default of eight rather than Rust's prior 64-chunk default.
- A configurable `runtime.managed_model_memory_budget_mib` soft budget defaults
  to 4096 MiB. After a completed chat turn, FileBot samples its and its own
  Ollama server's working sets and requests model unload at 90% of that budget;
  it does not kill the server, interrupt the answer, or change external Ollama.
  Shared pages may be counted twice. This is a conservative post-turn response,
  not an enforced cap; Windows threshold behavior and recovery latency need
  validation on supported hardware.
- The SQLite `LIKE` lexical fallback now matches filename terms individually
  and ranks exact filenames and stronger filename matches ahead of body-only
  hits. `%`, `_`, and backslash in user queries are escaped so SQLite treats
  them as literal text. After these changes it measured 35.69 ms p50 / 41.57 ms
  p95 at 10,000 synthetic chunks and 368.33 ms p50 / 401.31 ms p95 at 100,000
  chunks (21 searches per size, one matching row). Filename prioritization and
  literal matching add work to the fallback; 100k p95 rose from the prior
  281.15 ms baseline. This remains a synthetic in-memory fixture, not a
  disk-heavy personal-corpus benchmark or a defined latency SLA. No persistent
  FTS index has been added; larger or slower real corpora still need measurement.
- Ollama integration was retried on the local Windows host. The standard
  `11434` port is not visible in the current listener list, but Ollama itself
  reports it cannot bind because the address is already in use. Starting the
  managed server on free loopback port `11435` succeeds; FileBot's HTTP probe
  still receives `WinError 10013`, so inference cannot run in this restricted
  session. In that alternate-port run, FileBot peaked at 264.05 MiB working set
  / 586.07 MiB private commit and the idle Ollama server at 26.06 MiB / 53.18
  MiB; the combined sampled peaks were 290.11 MiB working set / 639.21 MiB
  private commit. No model loaded, quantization/GPU memory were unavailable, and
  system RAM (14,173 MiB total, 2,262 MiB minimum available) includes unrelated
  processes. These startup-only numbers do not validate the model memory target.
- `scripts/benchmark_chat.py` now samples FileBot and Ollama private commit and
  working set separately, Ollama loaded-model/VRAM estimates, whole-system RAM,
  wake/index duration, and cold/warm first-token and completion timing over a
  synthetic corpus without saving prompt or response text. In the current
  restricted Windows session, 25 synthetic 8 KiB files woke in 6.55 s and
  indexed in 22.94 s; FileBot peaked at 350.49 MiB working set and 711.53 MiB
  private commit. Ollama chat failed because Windows denied access to
  `127.0.0.1:11434` (WinError 10013), so these figures exclude Ollama and do not
  establish the product's 3.5–4 GB target or live latency.
- Background queue indexing runs in Windows background CPU and I/O priority,
  with bounded work batches and polling between batches.
- PDF, DOCX, Markdown, and text parsing feed FastEmbed/ONNX, ChromaDB, and BM25
  retrieval, with Reciprocal Rank Fusion and bounded indexing.
- SQLite lexical retrieval remains available when the vector embedder is missing
  or a vector query fails, so a model-backed file answer can still receive exact
  text matches during degraded local startup.
- Configured code extensions parse into syntax-aware Tree-sitter chunks with
  line ranges; setup caches grammars so indexing stays offline.
- The dedicated Search action queries the local index without Ollama. Chat
  requests retrieve relevant excerpts before generation and can call the local
  search tool for follow-up evidence. Prompt evidence is capped at 6,000
  characters and model tool results at ten excerpts to protect the configured
  local-model context window. Explicitly named indexed-file opens still bypass
  Ollama; file opening rechecks index membership and configured-root containment.
- Ask can now read a bounded excerpt from a specifically named indexed file,
  summarize a named file using a capped excerpt, or call a per-intent read-only
  file tool. Reads enforce configured-root and current-index checks and cap
  returned text at 12,000 characters. The single chat prompt routes Find to
  local results, creates only new bounded UTF-8 text/source files inside
  configured roots, and stages exact deletes behind a one-time chat confirmation
  before sending the unchanged file to Windows Recycle Bin. CMD remains
  proposal-only until the separate explicit UI approval action is taken.
- Chat can list a requested configured folder without model latency; the listing
  enforces root containment, skips links outside allowed roots, and bounds both
  the scan and displayed names.
- File opens and selected-result feedback persist to `file_signals` and gently
  influence future ranking. Search vocabulary can be edited in the popover;
  SQLite persists aliases and expands them before retrieval.
- Gated and blocked CMD and PowerShell proposals use shell-specific policies,
  SQLite audit records, and explicit Approve/Reject UI controls; only the
  approved runner executes them. PowerShell is currently limited to approved
  single cmdlets; chaining, scripts, network/install commands, process launch,
  and deletion remain blocked.
- Search result cards show paths, snippets, and available page/heading context.
- Windows Qt integration tests now cover chat-window and hotkey-bridge wake,
  selected-file open and feedback persistence, CMD audit approval/rejection,
  and idle resource unload. Existing Qt tests cover streamed chat and cancel.
- The desktop opens directly to a resizable chat window; the system tray menu
  opens or quits FileBot while the global hotkey toggles chat visibility.
- Chat stream deltas are coalesced into 30 ms UI updates, avoiding a Qt text
  widget mutation for every model fragment while keeping partial text visible.
- The popover now labels itself as a local assistant and retains the current
  session's user/assistant turns while a new answer streams. Up to 50 completed
  turns are now stored locally and restored after sleep/wake or app restart.
- Nanobot skill summaries are explicitly excluded from FileBot prompts because
  FileBot does not expose Nanobot's skill reader. This avoids a context-budget
  rejection caused by treating `"*"` as a literal disabled skill name.
- BM25 tokenization, indexing, and retrieval now suppress terminal progress
  bars. This prevents headless `pythonw` runs from failing on detached stdout
  and silently losing BM25 ranking.
- A local Ollama probe now verifies a real streamed response using an isolated
  temporary database and no user corpus. On the development Windows machine
  (`qwen2.5-coder:3b`), a cold run measured 23.687 s to first text / 23.834 s
  total; a subsequent warm run measured 6.019 s / 6.128 s. FileBot process
  working set rose 28.504→154.953 MB cold and 28.613→121.090 MB warm.
  These are single-turn observations, not peak system RAM or representative
  performance targets; Ollama's separate process is not included.

## Remaining product requirements

- Run relevance controls against a representative personal corpus. A
  deterministic SQLite lexical fixture now checks edited glossary expansion
  and same-session feedback ranking, but semantic ranking on a real local
  corpus remains unvalidated.
- Windows UI acceptance flows pass in CI on Python 3.11 and 3.12. A local live
  Ollama chat now returns a streamed response; hardware keyboard, startup
  cancellation, wake-to-idle, and repeatable multi-machine performance checks
  remain open.
- This Windows session passed 29 lifecycle/Qt tests, 14 file-operation,
  tool-bridge, startup, and watcher tests, 42 benchmark/chat/config/retrieval
  tests, and three targeted Ollama runtime tests. The local runner used Python
  3.13 (outside the supported release matrix), and this was focused coverage,
  not a full-suite or release-matrix acceptance run.
- The complete `tests/test_core.py` module remains unverified in this restricted
  session: its third test blocks before entering the test body while Python
  3.13 creates a Proactor event loop. The captured stack is in
  `socket.accept` → `asyncio.proactor_events._make_self_pipe`, consistent with
  the session's denied socket operations; an isolated UI/model integration run
  on the supported Windows test environment is still required.
- After filename fallback ranking and memory-profile parity changes, 45
  `test_core.py` tests passed with 21 asyncio/model cases deselected, and 88
  tests across benchmark, chat, config, retrieval, file operations, Ollama tool
  bridge, startup, watcher, and Qt lifecycle modules passed. Compileall and
  `git diff --check` passed. These focused runs used the local Python 3.13 plus
  the virtualenv package path; they do not replace the complete supported
  version Windows test matrix.
- Repeat cold startup, warm chat, indexing, peak total chatbot RAM, GPU/shared
  memory, dormant working set, and idle unload on supported Windows machines.
  The current benchmark cannot reach Ollama under this environment's socket
  policy; the PDF's RAM and latency targets remain goals, not measured claims.
- Complete Rust feature parity, including local model start/recovery, hybrid
  retrieval feedback/glossary behavior, accessible result opening, and Windows
  UI verification. The core now starts/rechecks local Ollama on model use,
  expands persisted glossary entries, ranks open/vote signals, and the native UI
  can record votes. A Rust glossary editor and Windows UI verification remain
  open. Startup cancellation is now wired through the UI/lifecycle/runtime and
  stops only a FileBot-owned server child during readiness polling. Readiness
  HTTP probes are capped at two seconds, then cancellation is checked again;
  Rust formatting, offline core/desktop checks, and Clippy pass. Cancellation
  tests are present but could not be executed because the test binary cannot
  link ONNX Runtime (`OrtGetApiBase` is unresolved in this environment). Keep Python as the default
  until parity and measurements pass.
- In this Windows session, Rust formatting, offline core/desktop `cargo check`,
  and both Clippy targets pass with `ORT_SKIP_DOWNLOAD=1`. Core tests compile
  through the test crate but cannot link because the ONNX Runtime library is
  unavailable (`OrtGetApiBase` unresolved); native Rust runtime behavior and
  Windows UI flows therefore remain unverified here.
- Package and validate a clean Windows install, including persistent settings,
  model setup, upgrades, and a signed installer before a general release.

## Next implementation order

1. Run relevance checks against exact filenames, short identifiers, semantic
   questions, feedback signals, and user-edited glossary entries.
2. Automate Windows end-to-end checks and gather real latency/RAM baselines.
3. Close Rust parity gaps and repeat the end-to-end and performance checks.
4. Package only after the supported Python and Rust acceptance gates pass.
