# FileBot 1.0.0-rc2

FileBot is a local Windows file search assistant with a small always-on avatar and an explicit approval step for gated CMD commands. The sleeping process keeps the PyQt UI, global hotkey, watchdog observer, and SQLite database available. Embeddings, ChromaDB, and the bundled local LLM engine (llama.cpp's `llama-server`) are loaded after wake and released on idle timeout.

See [CHANGELOG.md](CHANGELOG.md) for versioned updates.
See [AGENT.md](AGENT.md) for the architecture map, safety invariants, and development workflow.

## Demo mode (no models needed)

`python app.pyw --demo` (or `.\scripts\run_demo.ps1`) runs the real app on the bundled `demo/sample_files` with a separate database in `~/.filebot-demo`. Search is SQLite FTS5 only, and **Ask** answers by quoting the matching files, so no embedding model, GGUF or `llama-server.exe` is needed. CMD proposals still need an explicit Approve click. Your own files are never indexed in demo mode. `python scripts/demo_screens.py out/` renders every UI state to PNG without a display.

## Requirements

- Windows 10/11 and Python 3.11 or 3.12 (64-bit)
- A llama.cpp `llama-server.exe` Windows build and a GGUF chat model (see "Local model"); packaged builds will bundle both
- Internet access for the initial Python packages and the FastEmbed download; searches and inference run locally afterward

## Setup

From the repository folder in PowerShell:

```powershell
.\scripts\setup.ps1
```

Run `python scripts/fetch_models.py` once (needs network) to place the embedding model in `models/bge-small-en-v1.5-onnx`; FileBot loads it offline from there (`models.embeddings.model_dir`). If that folder is missing, FastEmbed falls back to downloading into `cache_dir` on first wake. Changing the embedding model, or upgrading from a version before 0.8.0, rebuilds the vector index once and re-embeds your files in the background. ChromaDB data, the SQLite database, and rotating logs (including `llama-server.log`) are stored under `~/.filebot` by default. Configured paths support `~`, `%VARIABLE%`, and `$VARIABLE` expansion.

### Local model

FileBot starts its own `llama-server` only while awake, bound to `127.0.0.1` on a free port, and stops it on sleep. Place the files where `models.llm` in `config/settings.yaml` points (relative paths resolve against the app folder):

- `bin/llama-server.exe` — a Windows release build of [llama.cpp](https://github.com/ggml-org/llama.cpp) (keep its DLLs beside it)
- `models/qwen2.5-3b-instruct-q4_k_m.gguf` — Qwen2.5 3B Instruct, Q4_K_M quantization (about 2 GB); any tool-capable GGUF works if you update `model_path`

Edit `config/settings.yaml` to choose folders to index, supported extensions, maximum file size, and `models.llm` (`server_path`, `model_path`, `n_ctx`, `threads`). The avatar hotkey defaults to **Ctrl+Alt+Space**. The initial watched roots default to `~/Documents`.

If **Ask** reports that the engine or model is missing, add the files above. If the engine exits during startup, the error includes the last lines of `~/.filebot/logs/llama-server.log`.

Start FileBot hidden in the signed-in desktop session with:

```powershell
.\scripts\start.ps1
```

For startup diagnostics, run `.\scripts\start.ps1 -Console`. FileBot also writes rotating logs to `~/.filebot/logs/filebot.log`. Keep FileBot in the signed-in desktop session so the avatar and global hotkey can receive input.

## Search and indexing

Watchdog records debounced file changes in SQLite using one bounded-memory debounce thread. It does not load an embedding model. Wake-up loads retrieval resources on a background thread and starts an indexing service that processes queued updates in bounded batches while awake. Failed files remain queued with exponential retry backoff (five seconds up to one hour); after 12 failed attempts a file is parked (no more retries until it changes) so one corrupt document cannot starve later files. Search and indexing share a lock so vector and lexical state cannot be changed during a query. Dense FastEmbed/ChromaDB results and lexical results from a SQLite FTS5 index (BM25-ranked, kept in sync by database triggers, never loaded into memory) are combined using reciprocal rank fusion, with one result per file. Plain text, Markdown, code, CSV/JSON, HTML, PDF, DOCX, XLSX, and PPTX are parsed into bounded chunks. Search, model calls, and approved CMD runs stay off the Qt event loop. Searches are local after first-time package/model downloads.

The indexer intentionally handles only extensions listed in settings. Watch events, queued updates, retrieval results, and file opening are constrained to the configured roots; symlinks that resolve outside those roots are ignored. File size is capped at 50 MB by default, extracted text at five million characters, and DOCX expanded archive contents at 100 MB to bound parser and chunk memory. These limits are configurable under `index` in settings.

### Indexing a whole machine

Set `index.scan_fixed_drives: true` (Windows) to add every fixed local drive to `index.roots`, or list roots yourself. Skip rules combine directory names (`exclude_dirs`), `fnmatch` patterns on directory names (`exclude_globs`), and absolute prefixes (`exclude_paths`, with `%SystemRoot%`, `%ProgramFiles%`, `%ProgramData%`, and `~/AppData` excluded by default; entries naming an undefined variable are ignored). On start and every `index.rescan_seconds` (and after the watcher's `index.max_pending_events` buffer overflows) a background scan queues new or changed files, skipping any whose size and modified time already match the index, and removes entries for files deleted while FileBot was not watching. Roots that are unreachable (for example an unplugged drive) keep their entries. The first scan of a large drive is I/O heavy; it resumes cheaply after a restart because queued work is durable and unchanged files are skipped. File hashes are calculated in 1 MB blocks. Embeddings are written in bounded batches controlled by `models.embeddings.batch_size` (64 by default), and stale vectors are removed only after the replacement chunk records are committed in SQLite. Changing the roots or extensions does not automatically remove old database records; records outside the current roots are hidden from search and cannot be opened. Remove the database under `~/.filebot` if you want to rebuild from scratch.

## CMD approval

`config/cmd_policy.yaml` is default-deny. Explicitly blocked patterns take priority. `read_only` and `gated` proposals are written to `cmd_audit` and shown in the popover; the user must click **Approve** before either can run. **Reject** records the decision without running it. The current policy is checked again immediately before execution, and a changed or unreadable policy blocks the command. Every execution records its exit code and bounded output tails. The model has no shell or filesystem tools; its only tools are FileBot search, file open, and command proposal, offered per detected intent.

After an unclean shutdown, approved commands that had not started return to pending and require another approval click. Commands already claimed by the runner are marked interrupted with an uncertain outcome; FileBot never automatically reruns them.

Treat policy edits as security-sensitive. Keep installer, privilege, shell chaining, redirection, and broad recursive deletion commands blocked.

## Run unit tests

```powershell
python -m unittest discover -s tests -v
```

The Windows GitHub Actions workflow runs the unit suite on Python 3.11 and 3.12, including Qt lifecycle tests. It installs only the test-time YAML, watchdog, and PyQt6 dependencies; the local model and vector database are not needed for these unit tests.

## Data and privacy

The file index, audit log, and vectors stay on this machine. Inference runs in a local `llama-server` process reachable only on the loopback interface. Initial dependency and embedding model downloads require network access. File contents are never sent to a remote model.

## Current scope

The current release supports the sleep/wake shell, local indexing and hybrid retrieval, compact UI, and gated command flow. Supported formats are plain text, Markdown, common source code, CSV/JSON/YAML/XML, HTML, PDF, DOCX, XLSX, and PPTX (extensions are opt-in via `index.include_extensions`). Filename-only indexing of other file types is not included yet.

## Building the Windows installer

The installer bundles the app, `llama-server.exe`, the chat GGUF and the embedding model, so end users need no Python, Ollama or downloads. From a Windows machine with Python 3.11/3.12 and [Inno Setup 6.3+](https://jrsoftware.org/isinfo.php):

```powershell
.\packaging\build.ps1 -LlamaServerDir C:\llama.cpp\build\bin -ModelPath C:\models\qwen2.5-3b-instruct-q4_k_m.gguf
# add -Deep to also load both models and query llama-server during the self-check
# add -SkipInstaller to stop after the staged folder in dist\FileBot
```

The script installs requirements, fetches the embedding model if missing, freezes the app with PyInstaller (`packaging/filebot.spec`, one-folder), stages `bin/` and `models/` under `dist\FileBot\_internal`, runs `FileBot.exe --selfcheck --require-assets` on the result, then compiles `packaging/filebot.iss` into `dist-installer\FileBot-Setup-<version>.exe`. Expect a 2.5-3 GB installer; if your Inno Setup version rejects a single file that large, set `DiskSpanning=yes` in the `.iss`.

Installed builds are per-user (no admin rights). Defaults ship read-only inside the app; edit `%APPDATA%\FileBot\settings.yaml` (created on first run) to override any setting, such as `index.roots`. Those overrides are merged over the defaults and survive upgrades. The index, logs and vectors stay in `%USERPROFILE%\.filebot` and are kept on uninstall.

`FileBot.exe --selfcheck [--require-assets] [--deep] [--selfcheck-out=report.json]` verifies imports, the database and FTS5, the command policy and the bundled files without opening the GUI; use it when reporting a broken install. CI freezes a model-less build on every push and runs the plain self-check (`frozen-build` job).
