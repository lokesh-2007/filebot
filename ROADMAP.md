# FileBot product roadmap

The attached **FileBot Local File & CMD Assistant Architecture & Research**
document is the product acceptance blueprint. Its recommendations are treated
as project requirements; a reference document does not override explicit user
instructions. Python remains the supported desktop app while the Rust port is
still being validated.

## Implemented in the supported Python app

- Dormant Qt avatar, global hotkey, watchdog watcher, and SQLite WAL queue run
  without importing the embedding or agent runtime at startup.
- Wake shows a loading popover immediately while retrieval initialization runs
  off the GUI thread; controls activate when the same card becomes ready.
- An in-flight local model request can be cancelled from the popover without
  blocking Qt's UI thread; cancellation covers Ollama preflight/startup and a
  configured deadline bounds a stalled model turn.
- Retrieval and Nanobot initialize on wake; Ollama starts on demand for model
  requests and unloads according to the configured keep-alive/lifecycle policy.
- Background queue indexing runs in Windows background CPU and I/O priority,
  with bounded work batches and polling between batches.
- PDF, DOCX, Markdown, and text parsing feed FastEmbed/ONNX, ChromaDB, and BM25
  retrieval, with Reciprocal Rank Fusion and bounded indexing.
- Configured code extensions parse into syntax-aware Tree-sitter chunks with
  line ranges; setup caches grammars so indexing stays offline.
- The dedicated Search action queries the local index without Ollama. Chat
  requests retrieve relevant excerpts before generation and can call the local
  search tool for follow-up evidence. Explicitly named indexed-file opens still
  bypass Ollama; file opening rechecks index membership and configured-root
  containment.
- File opens and selected-result feedback persist to `file_signals` and gently
  influence future ranking. Search vocabulary can be edited in the popover;
  SQLite persists aliases and expands them before retrieval.
- Gated and blocked CMD proposals use the policy file, SQLite audit records, and
  explicit Approve/Reject UI controls; only the approved runner executes them.
- Search result cards show paths, snippets, and available page/heading context.
- Windows Qt integration tests now cover avatar and hotkey-bridge wake,
  selected-file open and feedback persistence, CMD audit approval/rejection,
  and idle resource unload. Existing Qt tests cover streamed chat and cancel.
- Right-clicking the avatar now exposes a clean Quit action for controlled
  shutdown before a source-based update.
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
- Measure cold startup, wake-to-search, peak total system RAM, dormant working
  set, and idle unload on supported Windows machines. The PDF's RAM and latency
  targets remain goals, not measured claims.
- Complete Rust feature parity, including local model start/recovery, hybrid
  retrieval feedback/glossary behavior, accessible result opening, and Windows
  UI verification. The core now starts/rechecks local Ollama on model use,
  expands persisted glossary entries, ranks open/vote signals, and the native UI
  can record votes. A Rust glossary editor, cancellation during model-server
  startup, and Windows UI verification remain open. Keep Python as the default
  until parity and measurements pass.
- Package and validate a clean Windows install, including persistent settings,
  model setup, upgrades, and a signed installer before a general release.

## Next implementation order

1. Run relevance checks against exact filenames, short identifiers, semantic
   questions, feedback signals, and user-edited glossary entries.
2. Automate Windows end-to-end checks and gather real latency/RAM baselines.
3. Close Rust parity gaps and repeat the end-to-end and performance checks.
4. Package only after the supported Python and Rust acceptance gates pass.
