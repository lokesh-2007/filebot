# FileBot 0.6.14

FileBot is a local Windows file assistant with one resizable chat window for indexed-file search, reading, summaries, and bounded file actions. Find, create, read, summarize, open, and delete requests all start in the same prompt box. The process stays in the system tray with a global hotkey while heavyweight retrieval and chat resources load on demand and unload after idle.

See [CHANGELOG.md](CHANGELOG.md) for versioned updates.
See [AGENT.md](AGENT.md) for the architecture map, safety invariants, and development workflow.
See [ROADMAP.md](ROADMAP.md) for the attached architecture plan's implemented features and remaining product gates.

## Rust migration status

The native Rust migration is underway in [`rust/`](rust/). It now includes a
low-overhead Win32 avatar and popover preview wired to local search, streamed
Ollama chat, the watcher, idle unload, and explicit terminal approval. The Python
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

The FastEmbed embedding model is downloaded on the first wake into the cache directory in `config/settings.yaml`. Setup also caches Nanobot's `cl100k_base` tokenizer in `~/.filebot/nanobot/cache/tiktoken`; this avoids the byte-count fallback rejecting an ordinary first chat turn while the tokenizer downloads in the background. Initial setup therefore needs network access for packages, tokenizer data, and model assets. Setup attempts to cache the configured Tree-sitter grammars; if GitHub is unavailable, setup warns and source indexing uses bounded text chunks until the grammars are cached. Indexing and wake never download grammars. ChromaDB data, the SQLite database, rotating logs, and Nanobot state are stored under `~/.filebot` by default. Configured paths support `~`, `%VARIABLE%`, and `$VARIABLE` expansion.

Edit `config/settings.yaml` to choose folders to index, supported extensions, maximum file size, model, Ollama URL, `keep_alive`, and `runtime.request_timeout_seconds` (180 seconds by default). Set `keep_alive` to `"0"` for immediate Ollama unload or `"2m"` to allow a two-minute model cache. The **Stop** button cancels the model preflight, Ollama startup wait, or chat request. The global hotkey defaults to **Ctrl+Alt+Space**. The initial watched roots default to `~/Documents` and `~/Downloads`; on Windows, `~/Documents` follows the OneDrive redirection when it is configured.

The default memory profile keeps Ollama to one loaded model and one parallel request, caps model context at 4,096 tokens, and uses smaller embedding/index batches. `runtime.managed_model_memory_budget_mib` defaults to 4096; after a chat turn finishes, FileBot checks the FileBot plus Ollama resident working-set sum and asks its own managed Ollama server to unload the model when that sum reaches 90% of the soft budget. It never terminates the server or interrupts an active turn. Shared working-set pages may be counted twice, so this is a conservative trigger, not a memory cap. This response applies only when FileBot started the Ollama server. If Ollama is already running as a separate Windows app or service, configure that server with `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`, and `OLLAMA_CONTEXT_LENGTH=4096`; FileBot cannot change an external server's process settings. GPU memory, drivers, corpus size, and other applications remain outside this soft budget. Measure peak memory on the target PC before release.

To measure an isolated synthetic corpus without recording prompts or answers, run `\.venv\Scripts\python.exe scripts\benchmark_chat.py`. It samples FileBot and Ollama private commit/working set, Ollama's loaded-model and VRAM estimates, whole-system physical memory, wake/index time, and cold/warm chat timing. The script uses a temporary database/vector store; embedding downloads require the explicit `--allow-model-download` flag. If Ollama's default port is occupied, `--ollama-url http://127.0.0.1:11435` tests a different loopback port; FileBot binds its managed server to the configured host. A live synthetic run on 2026-11-28 reached the external Ollama model and completed both turns, but the CPU-only `qwen2.5-coder:3b` took 60.72 s to first text cold and 63.59 s warm; this is a measured limitation, not a responsiveness pass. That measurement predates the new retrieval-distance filter and contained irrelevant synthetic excerpts in its prompt. A smaller 10-file run after adding the vector cutoff still took 60.14 s cold / 56.51 s warm; it exposed a separate SQLite fallback issue where common question words such as “the” matched every synthetic file. Common English stopwords are now excluded from lexical terms, and an actual 2-file synthetic index returns zero hits for that unrelated prompt. The final stopword change has not had a new live Ollama benchmark. FileBot private commit peaked at 804.87 MiB and working set at 446.91 MiB in the 100-file run; Ollama process private commit/working set peaked at 60.08/33.46 MiB, while `/api/ps` reported 2,067.92 MiB model allocation and 0 MiB VRAM. Whole-system available RAM fell to 398.47 MiB on this 14,173.37 MiB machine. These values are separate measurements and must not be added as if they were disjoint. The service was an already-running external Ollama process, so idle model unload/reload and FileBot-managed memory recovery were not tested. Per-process working-set sums may double-count shared pages, Ollama's VRAM field is not total GPU/shared memory, and system memory includes other applications. The default vector context filter excludes Chroma cosine distances above `retrieval.max_vector_distance` (0.4) from chat excerpts; lexical matches remain usable and ignore common English stopwords. Both mechanisms need additional validation against representative personal corpora.

Use the single chat box for every request: “find the launch plan” returns local results; “read Project Notes.txt” returns a bounded indexed excerpt; “summarize Project Notes.txt” asks the local model for a summary; “what’s in Reports?” lists a configured folder; and “open Downloads” opens it directly in Explorer without consuming model context. Natural folder names such as “open chat gpt folder” are resolved inside the configured roots with bounded name matching; ambiguous matches are reported rather than guessed. A Windows test also verifies a Documents root redirected into OneDrive. You can ask FileBot to create a new `.txt`, `.md`, or supported source file from your prompt. “Create a text file named h2.txt” creates an empty file immediately; model-backed writing is used when you ask FileBot to draft content. Creation is limited to configured roots, UTF-8 text, 256 KB, and never overwrites an existing file. To delete, say “delete Project Notes.txt”; FileBot shows the exact path and a one-time code. Reply `CONFIRM DELETE <code>` to move that unchanged file to Windows Recycle Bin, or `CANCEL DELETE <code>`. The code expires after ten minutes. File listing, creation, reading, folder opening, and deletion stay within configured roots. Explicit requests such as “open Lab3.pdf” safely open an exact indexed file; duplicate filenames are shown as choices. Reading uses the current local index, so wait for a new or changed file to finish indexing before asking about its latest contents. CMD and allowlisted PowerShell requests are staged for visible approval; deletion, scripts, chaining, and network/install commands remain blocked in PowerShell. FileBot starts Ollama on demand on Windows by default (`models.ollama.auto_start: true`) and waits up to `startup_timeout_seconds` (30 by default). Use **Stop** to cancel a model request that is taking too long. If Ollama cannot be found, start it from the Start menu. If the configured model is missing, run `ollama pull qwen2.5-coder:3b` (or pull the model name you set in `settings.yaml`).

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

The indexer intentionally handles only extensions listed in settings. Default code extensions are parsed with Tree-sitter into syntax-aware chunks with line ranges; plain text and Markdown, PDF, and DOCX use their bounded document parsers. Grammars are cached by setup; if a grammar is unavailable, FileBot falls back to bounded plain-text chunks without attempting a network download. The Windows indexing thread runs at background CPU and I/O priority with bounded batches. Watch events, queued updates, retrieval results, and file opening are constrained to the configured roots; symlinks that resolve outside those roots are ignored. File size is capped at 50 MB by default, extracted text at five million characters, and DOCX expanded archive contents at 100 MB to bound parser and chunk memory. These limits are configurable under `index` in settings. File hashes are calculated in 1 MB blocks. Embeddings are written in batches of eight by default, and stale vectors are removed only after replacement chunk records are committed in SQLite. The in-memory BM25 index is capped at 10,000 chunks; larger corpora use persistent vector search and SQLite lexical fallback to avoid an unbounded Python token index. The fallback matches filename terms individually, ranks exact and stronger filename matches ahead of body-only results, and treats `%` and `_` literally. Its latest synthetic Windows measurement was 35.69 ms p50 / 41.57 ms p95 at 10,000 chunks and 368.33 ms p50 / 401.31 ms p95 at 100,000 chunks. These temporary in-memory fixtures are baselines, not a real-corpus latency SLA. Use **Search vocabulary…** in the popover to add, edit, or remove shorthand such as `spec: product specification`; edits are stored in SQLite and apply immediately. Opening files and rating a selected result updates `file_signals`, which gently influences later search ranking. Changing the roots or extensions does not automatically remove old database records; records outside the current roots are hidden from search and cannot be opened. Remove the database under `~/.filebot` if you want to rebuild from scratch.

## Terminal approval

`config/cmd_policy.yaml` is default-deny. CMD actions are proposed as before. Requests such as “use PowerShell to list Downloads” can propose one allowlisted Windows PowerShell 5.1 cmdlet: `Get-ChildItem`, `Get-Content`, `Get-Location`, `Get-Process`, `Get-Service`, `Get-Date`, `Get-Command`, or `Get-Help`. Basic folder creation and `Copy-Item`, `Move-Item`, and `Rename-Item` are gated actions. Every CMD and PowerShell proposal is written to `cmd_audit`, shown with its shell and risk, and requires **Approve** before it can run. **Reject** records the decision without running it. The current shell-specific policy is checked again immediately before execution, and a changed or unreadable policy blocks the command. Every run records its exit code and bounded output tails.

PowerShell is intentionally limited to a single allowlisted cmdlet per proposal. Pipelines, chaining, redirection, script execution, subexpressions, network/install/process-launch operations, and PowerShell deletion are blocked. Use FileBot's exact-file delete flow to move an unchanged file to the Windows Recycle Bin after its separate confirmation code. Nanobot's general shell and filesystem tools remain disabled; its registered terminal tools only stage proposals for the visible approval UI.

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
