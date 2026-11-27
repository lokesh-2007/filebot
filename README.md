# FileBot 0.6.14

FileBot is a local Windows file assistant with one resizable chat window for indexed-file search, reading, summaries, and bounded file actions. Find, create, read, summarize, open, and delete requests all start in the same prompt box. The process stays in the system tray with a global hotkey while heavyweight retrieval and chat resources load on demand and unload after idle.

See [CHANGELOG.md](CHANGELOG.md) for versioned updates.
See [AGENT.md](AGENT.md) for the architecture map, safety invariants, and development workflow.
See [ROADMAP.md](ROADMAP.md) for the attached architecture plan's implemented features and remaining product gates.

## Rust migration status

The native Rust migration is underway in [`rust/`](rust/). It now includes a
low-overhead Win32 avatar and popover preview wired to local search, streamed
Ollama chat, the watcher, idle unload, and explicit CMD approval. The Python
0.6.14 app remains the supported desktop release while the Rust UI is tested on
Windows and measured for startup, responsiveness, and memory use. See
[`rust/README.md`](rust/README.md) for build notes and remaining parity checks.

## Requirements

- Windows 10/11 and Python 3.11 or 3.12 (64-bit)
- [Ollama for Windows](https://ollama.com/download)
- Internet access for the initial Python packages and model downloads; searches and inference run locally afterward

## Setup

From the repository folder in PowerShell:

```powershell
.\scripts\setup.ps1
# Or add -DownloadModel to also pull the roughly 2 GB Ollama model:
.\scripts\setup.ps1 -DownloadModel
# Or download it later with:
ollama pull qwen2.5-coder:3b
```

The FastEmbed embedding model is downloaded on the first wake into the cache directory in `config/settings.yaml`. Setup attempts to cache the configured Tree-sitter grammars; if GitHub is unavailable, setup warns and source indexing uses bounded text chunks until the grammars are cached. Indexing and wake never download grammars. ChromaDB data, the SQLite database, rotating logs, and Nanobot state are stored under `~/.filebot` by default. Configured paths support `~`, `%VARIABLE%`, and `$VARIABLE` expansion.

Edit `config/settings.yaml` to choose folders to index, supported extensions, maximum file size, model, Ollama URL, `keep_alive`, and `runtime.request_timeout_seconds` (180 seconds by default). Set `keep_alive` to `"0"` for immediate Ollama unload or `"2m"` to allow a two-minute model cache. The **Stop** button cancels the model preflight, Ollama startup wait, or chat request. The global hotkey defaults to **Ctrl+Alt+Space**. The initial watched roots default to `~/Documents`.

Use the single chat box for every request: “find the launch plan” returns local results; “read Project Notes.txt” returns a bounded indexed excerpt; “summarize Project Notes.txt” asks the local model for a summary; and “what’s in Reports?” lists a configured folder. You can also ask FileBot to create a new `.txt`, `.md`, or supported source file from your prompt. Creation is limited to configured roots, UTF-8 text, 256 KB, and never overwrites an existing file. To delete, say “delete Project Notes.txt”; FileBot shows the exact path and a one-time code. Reply `CONFIRM DELETE <code>` to move that unchanged file to Windows Recycle Bin, or `CANCEL DELETE <code>`. The code expires after ten minutes. File listing, creation, reading, and deletion stay within configured roots. Explicit requests such as “open Lab3.pdf” safely open an exact indexed file; duplicate filenames are shown as choices. Reading uses the current local index, so wait for a new or changed file to finish indexing before asking about its latest contents. CMD actions still require the separate visible approval. FileBot starts Ollama on demand on Windows by default (`models.ollama.auto_start: true`) and waits up to `startup_timeout_seconds` (30 by default). Use **Stop** to cancel a model request that is taking too long. If Ollama cannot be found, start it from the Start menu. If the configured model is missing, run `ollama pull qwen2.5-coder:3b` (or pull the model name you set in `settings.yaml`).

Start FileBot hidden in the signed-in desktop session with:

```powershell
.\scripts\start.ps1
```

For startup diagnostics, run `.\scripts\start.ps1 -Console`. FileBot also writes rotating logs to `~/.filebot/logs/filebot.log`, including request duration, time-to-first-token, wake initialization, and Windows working-set snapshots. Prompts and model text are not included in these metrics. Keep FileBot in the signed-in desktop session so the tray icon and global hotkey can receive input.

Use the system tray menu to open or quit FileBot. For a source-checkout upgrade, use a clean `main`
checkout and run `git pull --ff-only origin main`, then rerun
`.\scripts\setup.ps1`. Setup reuses a compatible Python 3.11/3.12 virtual
environment and updates its dependencies. The SQLite index, command audit,
and logs stay under `~/.filebot`; the embedding model stays in its configured
cache. Back up any customized `config/settings.yaml` before updating. FileBot
currently uses a source-based setup; a signed installer and automated installer
upgrade remain release gates.

## Search and indexing

Watchdog records debounced file changes in SQLite using one bounded-memory debounce thread. It does not load an embedding model. Wake-up loads retrieval resources on a background thread and starts an indexing service that processes queued updates in bounded batches while awake. Failed files remain queued with exponential retry backoff (five seconds up to one hour); a new change makes them eligible immediately, so one corrupt document cannot starve later files. Search and indexing share a lock so vector and lexical state cannot be changed during a query. Dense FastEmbed/ChromaDB results and BM25 results are combined using reciprocal rank fusion. Plain text, Markdown, PDF, and DOCX are parsed into bounded chunks. Search, model calls, and approved CMD runs stay off the Qt event loop. Searches are local after first-time package/model downloads.

The indexer intentionally handles only extensions listed in settings. Default code extensions are parsed with Tree-sitter into syntax-aware chunks with line ranges; plain text and Markdown, PDF, and DOCX use their bounded document parsers. Grammars are cached by setup; if a grammar is unavailable, FileBot falls back to bounded plain-text chunks without attempting a network download. The Windows indexing thread runs at background CPU and I/O priority with bounded batches. Watch events, queued updates, retrieval results, and file opening are constrained to the configured roots; symlinks that resolve outside those roots are ignored. File size is capped at 50 MB by default, extracted text at five million characters, and DOCX expanded archive contents at 100 MB to bound parser and chunk memory. These limits are configurable under `index` in settings. File hashes are calculated in 1 MB blocks. Embeddings are written in bounded batches controlled by `models.embeddings.batch_size` (16 by default), and stale vectors are removed only after the replacement chunk records are committed in SQLite. Use **Search vocabulary…** in the popover to add, edit, or remove shorthand such as `spec: product specification`; edits are stored in SQLite and apply immediately. Opening files and rating a selected result updates `file_signals`, which gently influences later search ranking. Changing the roots or extensions does not automatically remove old database records; records outside the current roots are hidden from search and cannot be opened. Remove the database under `~/.filebot` if you want to rebuild from scratch.

## CMD approval

`config/cmd_policy.yaml` is default-deny. Explicitly blocked patterns take priority. `read_only` and `gated` proposals are written to `cmd_audit` and shown in the popover; the user must click **Approve** before either can run. **Reject** records the decision without running it. The current policy is checked again immediately before execution, and a changed or unreadable policy blocks the command. Every execution records its exit code and bounded output tails. Nanobot's general shell and filesystem tools are disabled; its registered tools are FileBot search, file open, and command proposal only.

After an unclean shutdown, approved commands that had not started return to pending and require another approval click. Commands already claimed by the runner are marked interrupted with an uncertain outcome; FileBot never automatically reruns them.

Treat policy edits as security-sensitive. Keep installer, privilege, shell chaining, redirection, and broad recursive deletion commands blocked.

## Run unit tests

```powershell
python -m unittest discover -s tests -v
```

The Windows GitHub Actions workflow runs unit and desktop acceptance tests on
Python 3.11 and 3.12. Separate jobs install production requirements and check
lazy startup and Chroma shutdown. Tests use local fixtures and do not require a
personal corpus or a live Ollama server.

## Data and privacy

The file index, audit log, vectors, conversation history (up to 50 completed turns), and Nanobot workspace stay on this machine. Ollama inference uses the configured local endpoint. Initial dependency, Ollama model, and embedding model downloads require network access. File contents are not sent to a remote model by FileBot's configured Ollama route.

## Current scope

The current release supports the sleep/wake shell, local indexing and hybrid retrieval, compact UI, and gated command flow. Supported document formats are text, Markdown, PDF, and DOCX; configured source extensions use Tree-sitter chunks when their grammars are cached. A signed installer package is not included yet.
