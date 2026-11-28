# Changelog

Version numbers follow semantic versioning. This project is pre-1.0; minor
versions may still change behavior while the Windows release is stabilized.

## 1.0.0-rc4 (window controls)
- Popover header now has **–** Minimise (hide the panel, stay ready; avatar click or Ctrl+Alt+Space reopens it, state kept until the idle timeout), **×** Close (hide and unload models, avatar stays) and **Quit** End task (quits the whole process: watcher, hotkey and `llama-server`). Esc minimises.
- Right-click the avatar for "Open / close FileBot" and "Quit FileBot (end task)", so FileBot can be ended even if the panel will not open.
- `LifecycleManager.minimize/request_quit`, `quit_requested` signals wired to `QApplication.quit` in `app.pyw`; `tests/test_window_controls.py` (7 tests).

## 1.0.0-rc3 (working build on your own files)
- Auto mode (`service_factory.py`, `runtime.mode: auto|lite|full`): real folders are indexed without any model; semantic search and the local LLM switch on automatically when their files are present, and fall back to keyword search/quoted answers if they fail to start. Popover header shows the active mode.
- Default roots: missing roots are ignored; `%OneDrive%\Documents` is added when the default `~/Documents` is used.
- `Setup.bat`, `FileBot.bat`, `FileBot-Console.bat` launch without PowerShell execution-policy errors. `setup.ps1` no longer dies when `py -3.11` is not installed (Windows PowerShell 5.1 stderr bug) and tries 3.12 first.
- `--demo` is unchanged (sandboxed sample files).

## 1.0.0-rc2 (demo front end)
- New popover design (`ui/theme.py`, `ui/result_delegate.py`): status pill, result cards with file-type badges and highlighted matches, approval card, trust chips; animated avatar with state ring.
- Demo mode (`--demo`, `demo_support.py`): sample corpus, `LexicalEngine` (FTS5 only), `DemoServer` stand-in for `llama-server` (extractive answers, `dir`/`whoami`/`ipconfig` proposals through the real approval flow).
- `scripts/demo_screens.py` renders UI states offscreen; `scripts/run_demo.ps1` launches the demo.
- Verified on Linux: full test suite (new `tests/test_demo.py`), offscreen screenshots. Not verified: Windows hotkey, real model.

## 1.0.0-rc1 — 2026-11-27

- Packaging: PyInstaller one-folder spec (`packaging/filebot.spec`), Inno Setup script
  (`packaging/filebot.iss`, per-user install, optional start with Windows) and
  `packaging/build.ps1`, which freezes, stages `llama-server`, the GGUF and the
  embedding model, self-checks, and builds the installer.
- `app.pyw` resolves its root through `resources.app_root()` and no longer changes the
  working directory. Settings: a writable `%APPDATA%\FileBot\settings.yaml` is merged
  over the read-only defaults (created on first run of a frozen build).
- New `FileBot.exe --selfcheck [--require-assets] [--deep] [--selfcheck-out=...]`, plus a
  `frozen-build` CI job that freezes a model-less build, self-checks it and uploads it.
- Verified: a real PyInstaller freeze on Linux (900 MB without models) whose
  self-check imported every runtime and lazily loaded first-party module, opened the
  FTS5 database and found the bundled config. Not verified: anything on Windows
  (`build.ps1`, Inno Setup, installer size, pynput backend, `--deep` with real models).

## 0.9.0 — 2026-11-27

- Lexical search now uses a SQLite FTS5 index (BM25-ranked, filename-weighted) kept in
  sync by triggers; the in-memory `bm25s` corpus and the `bm25s` dependency are gone.
  Existing databases are backfilled on first start. A 200k-chunk benchmark answered
  broad queries in about 0.2 s. Vector hits are resolved through SQLite so stale
  vectors and out-of-root paths no longer appear, and results are deduplicated per file.
- New `IndexScope`: roots resolved once, plus `exclude_dirs`, `exclude_globs`,
  `exclude_paths` and opt-in `scan_fixed_drives`. Watcher events from excluded
  trees are rejected before any `resolve()`.
- Scanner: skips files whose size and mtime match the index, removes entries for files
  deleted while offline (never for unreachable roots), rescans every `rescan_seconds`
  and after event-buffer overflow (`max_pending_events`).
- Files failing 12 times are parked instead of retried forever; a new change retries them.
- New parsers: source code and data files, HTML, XLSX, PPTX. Not done: filename-only
  indexing of unparsed types, and a UI for scan progress (`FileWatcher.progress` exists).
- Verified on Linux only (unit tests, a real-SQLite benchmark). Not run on a real
  whole-drive Windows scan.

## 0.8.0 — 2026-11-26

- Load the embedding model offline from a bundled ONNX folder
  (`models.embeddings.model_dir`, via FastEmbed `specific_model_path` with
  `local_files_only`). A missing or empty folder falls back to the download cache
  with a warning. `scripts/fetch_models.py` populates the folder in source checkouts.
- Disable Chroma anonymized telemetry.
- Stamp the vector collection with the embedding model name and dimension. A
  mismatch, or a pre-0.8.0 unstamped collection, drops the vectors and requeues
  every indexed file for re-embedding (`Database.requeue_all_files`).
- Tests use fake fastembed/chromadb modules; the Chroma collection calls were also
  checked against the real chromadb 1.5.9. The bundled-folder load itself and
  `fetch_models.py` were not run against the real model (Hugging Face is blocked here).

## 0.7.0 — 2026-11-26

- Replace Ollama and Nanobot with a bundled llama.cpp `llama-server` sidecar:
  loopback-only on a free port, started on first model use, stopped on sleep,
  and tied to FileBot's lifetime by a kill-on-close Job Object on Windows.
  Engine output goes to `~/.filebot/logs/llama-server.log` and startup errors
  include its last lines.
- New bounded (up to five steps) tool loop in `agent/bot_runtime.py`. Per-intent
  tool scoping, propose-only CMD, and the unexecuted-tool-JSON guard are kept;
  plain-text JSON tool sketches are still converted and never shown to the user.
- Settings: `models.ollama` becomes `models.llm` (`server_path`, `model_path`,
  `n_ctx`, `threads`). Bundled policy/config paths resolve through `resources.py`.
- Remove `ollama` and `nanobot-ai` from requirements and CI.
- Tests: add a fake `llama-server` and runtime/server tests (tool loop, sketch
  bridging, cancel, startup failure); drop the Nanobot/Ollama tests.
- Verified on Linux with the fake engine only. A real Windows run with the real
  `llama-server.exe` and GGUF model has not been done.

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
