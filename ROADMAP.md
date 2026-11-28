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
  and model prompts ask for natural, concise, result-first replies. A live
  2026-11-28 benchmark completed two turns but measured 60.72 s cold and 63.59 s
  warm to first text on CPU-only `qwen2.5-coder:3b`; chat responsiveness is still
  below product expectations and needs hardware/model tuning.
- Setup now prefetches Nanobot's `cl100k_base` tokenizer into FileBot's data
  directory; runtime waits up to three seconds for tokenizer initialization
  before accepting a turn. This fixes first-turn context checks falling back to
  UTF-8 byte counts and rejecting ordinary prompts. Regression tests cover
  successful and unavailable tokenizer startup. If setup cannot download the
  tokenizer, the fallback remains conservative and may still reject prompts.
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
- Vector search now requests Chroma cosine distances and excludes unscored or
  weak vector hits above configurable `retrieval.max_vector_distance` (default
  0.4) from fused chat/search results; BM25/SQLite lexical hits remain eligible.
  A regression fixture verifies unrelated vector results are dropped. A small
  FastEmbed check measured distance 0.445 for neutral synthetic text versus a
  single-word chat prompt and 0.248 for a matching warranty question/document;
  this supports the initial cutoff but is not broad relevance validation.
- Historical restricted-session Ollama integration showed the standard
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
  synthetic corpus without saving prompt or response text. Earlier restricted
  runs used 25 synthetic 8 KiB files, woke in 6.55 s, indexed in 22.94 s, and
  could not reach Ollama (`WinError 10013`); those startup-only results were
  superseded by the live benchmark below and did not validate model memory.
- Live external-Ollama benchmark, 2026-11-28: `qwen2.5-coder:3b`, Q4_K_M,
  4096 context, 100 synthetic 16 KiB files. Wake 4.22 s; embedding/indexing
  163.69 s; cold first text 60.72 s / complete 60.82 s; warm first text 63.59 s
  / complete 63.70 s. FileBot peak working set/private commit was 446.91/804.87
  MiB; Ollama process peak was 33.46/60.08 MiB; Ollama `/api/ps` model allocation
  was 2067.92 MiB and VRAM 0 MiB. System RAM total was 14173.37 MiB, with only
  398.47 MiB available at the lowest sample. The two model turns succeeded, but
  the 3.5–4 GB target and responsiveness were not accepted: model-allocation
  estimates are distinct from process counters, unrelated apps consumed RAM,
  the host was nearly out of available RAM, Ollama was externally owned, and
  idle unload/reload was not tested. This run occurred before the vector
  distance filter and supplied irrelevant synthetic excerpts to the prompt.
- A second live run with 10 synthetic 1 KiB files woke in 4.63 s and indexed in
  1.54 s. Cold/warm time to first text was 60.14/56.51 s and total completion
  was 60.26/56.63 s; preflight was 22 ms then cached, while retrieval took
  35–47 ms. FileBot peak working set/private commit was 350.07/667.77 MiB;
  Ollama process peak was 33.41/59.72 MiB; model allocation remained 2067.92
  MiB with 0 MiB VRAM; system RAM available bottomed at 443.59 MiB. Both turns
  returned `READY`, but the 2,088-token input still included irrelevant excerpts:
  Chroma distances were above 0.4, while SQLite matched the stopword “the” in
  every file. The lexical fallback now removes common English question
  stopwords. An actual post-fix 2-file indexed synthetic check returns zero
  results for “Reply with the single word READY.” The final retrieval changes
  have unit tests and this local relevance check, but no new live-model run.
  Chat remains far too slow on this CPU-only host; model-speed tuning and a
  broader Windows hardware matrix remain release blockers.
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
- Direct folder open now accepts configured root names without the word “folder”
  (for example “open downloads”) and uses bounded depth-three name matching
  inside configured roots for spacing/punctuation variants such as “chat gpt”.
  Ambiguous normalized names return candidate paths without opening either.
- A named create request such as “create a text file named h2.txt” now creates
  an empty file directly, without waiting on Ollama. Explicit user-provided text
  is written directly; requests to draft/author file contents still use the
  model tool. Existing root containment, extension, size, and no-overwrite
  checks apply. Chat, file-operation, and router regression tests cover both
  screenshots' failures.
- The fast router also recognizes tested natural variants for local actions:
  “locate my plan,” “what does guide.txt say?,” “what’s inside Downloads?,”
  “bring up Downloads,” and “generate a new file called plan.md.” How-to
  questions remain model conversations, while direct CMD and PowerShell
  requests still enter their existing explicit approval flow. This tested
  phrase set improves coverage but does not imply understanding every possible
  wording; unmatched phrases remain conversational requests.
- CMD is reachable from the chat prompt with `cmd: <command>`; “open cmd prompt”
  displays the syntax, a visible **CMD** button inserts the command prefix, and
  common bare commands such as `dir` are routed directly.
  Those requests bypass model latency but only stage an auditable, visible
  approval card. Unknown single commands are approval-gated; chained commands,
  shell expansion, destructive terminal deletion, installers, and privileged or
  disk-management operations stay blocked. This is one approved command per
  turn rather than a persistent interactive console. PowerShell keeps its
  narrower single-cmdlet allowlist. Near-miss corrections cover tested action
  words and do not rewrite dotted filenames. The desktop entry point now creates
  and wires the always-on-top robot avatar; the drawing class alone was not
  enough to make it visible in the running app.
- Delete requests now have a visible exact-path confirmation card with separate
  Recycle Bin and Cancel actions. File summaries may parse bounded text directly
  from one exact configured-root file before indexing catches up; missing,
  ambiguous, unsupported, and over-limit files must return a clear error rather
  than sending that error text to the model as source content. Interactive native
  CMD, broad multi-command scripts, and high-risk shell operations remain outside
  this approval-gated chat flow.
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
- A subsequent local Windows run on Python 3.13 passed 85 tests across the
  repository virtualenv's config (11), retrieval (23), startup (1), watcher
  (4), chat-context (8), file-operation (6), Ollama bridge (3), and Qt lifecycle
  (29) modules. Nine additional `test_core.py` direct file/approval cases and
  the OneDrive redirected Documents folder-open case passed separately. The
  retrieval run corrected an environment-dependent test: missing `bm25s` is
  now explicitly simulated. These focused tests do not replace Python 3.11/3.12
  or full-suite acceptance.
- Repeat cold startup, warm chat, indexing, peak total chatbot RAM, GPU/shared
  memory, dormant working set, and idle unload on supported Windows machines.
  The current benchmark cannot reach Ollama under this environment's socket
  policy; the PDF's RAM and latency targets remain goals, not measured claims.
- A fresh loopback check on 28 November 2026 found no listeners on ports 11434
  or 11435, and Windows rejected both HTTP probes with a socket-permissions
  error. This sandbox therefore cannot provide a new live cold/warm Ollama run,
  managed unload/reload validation, or full-stack RAM/GPU measurement.
- Complete Rust feature parity. Rust currently supports indexed search/open and
  explicit CMD proposals. Bounded direct read/summarize, configured-folder
  listing/opening, safe file creation, exact-target confirmed recoverable
  deletion, the separate PowerShell proposal gate, accessible UI, and Windows
  visual/interaction verification remain open. The core now starts/rechecks
  local Ollama on model use, expands persisted glossary entries, ranks open/vote
  signals, and the native UI can record votes and edit the SQLite glossary.
  Startup cancellation is wired through the UI/lifecycle/runtime and stops only
  a FileBot-owned server child during readiness polling. Readiness HTTP probes
  are capped at two seconds, then cancellation is checked again;
  Rust formatting, offline core/desktop checks, and Clippy pass. All 57 core
  Rust tests and both desktop transcript tests now pass using a temporary
  import library for the installed ONNX Runtime 1.32.0 DLL; this does not
  validate live Ollama or native UI interaction. Keep Python as the default
  until parity and measurements pass.
- Rust chat streaming now appends deltas through the native edit control rather
  than rebuilding the whole transcript per delta, then trims from 30,000 to
  24,000 UTF-16 units while preserving complete Unicode characters. Pure trim
  tests pass; native interaction remains unverified.
- In this Windows session, Rust formatting, offline core/desktop `cargo check`,
  both Clippy targets, and Rust tests pass. The tests used the existing Python
  environment's ONNX Runtime DLL with a temporary import library and no
  downloads. Live Ollama behavior, model RAM/latency, and interactive native UI
  flows remain unverified.
- Package and validate a clean Windows install, including persistent settings,
  model setup, upgrades, and a signed installer before a general release.

## Next implementation order

1. Run relevance checks against exact filenames, short identifiers, semantic
   questions, feedback signals, and user-edited glossary entries.
2. Automate Windows end-to-end checks and gather real latency/RAM baselines.
3. Close Rust parity gaps and repeat the end-to-end and performance checks.
4. Package only after the supported Python and Rust acceptance gates pass.
