# FileBot

FileBot is a local Windows file search assistant with a small always-on avatar and an explicit approval step for gated CMD commands. The sleeping process keeps the PyQt UI, global hotkey, watchdog observer, and SQLite database available. Embeddings, ChromaDB, BM25, and Nanobot are loaded after wake and released on idle timeout.

## Requirements

- Windows 10/11 and Python 3.11 or 3.12 (64-bit)
- [Ollama for Windows](https://ollama.com/download)
- Internet access for the initial Python packages and model downloads; searches and inference run locally afterward

## Setup

From the repository folder in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
ollama pull qwen2.5-coder:3b
```

The FastEmbed embedding model is downloaded on the first wake into the cache directory in `config/settings.yaml`. ChromaDB data, the SQLite database, rotating logs, and Nanobot state are stored under `~/.filebot` by default. Configured paths support `~`, `%VARIABLE%`, and `$VARIABLE` expansion.

Edit `config/settings.yaml` to choose folders to index, supported extensions, maximum file size, model, Ollama URL, and `keep_alive`. Set `keep_alive` to `"0"` for immediate Ollama unload or `"2m"` to allow a two-minute model cache. The avatar hotkey defaults to **Ctrl+Alt+Space**. The initial watched roots default to `~/Documents`.

Start FileBot with:

```powershell
pythonw .\app.pyw
```

For startup diagnostics, use `python .\app.pyw` from a console. FileBot also writes rotating logs to `~/.filebot/logs/filebot.log`. Keep FileBot in the signed-in desktop session so the avatar and global hotkey can receive input.

## Search and indexing

Watchdog records debounced file changes in SQLite using one bounded-memory debounce thread. It does not load an embedding model. Wake-up loads retrieval resources on a background thread and starts an indexing service that processes queued updates in bounded batches while awake. Search and indexing share a lock so vector and lexical state cannot be changed during a query. Dense FastEmbed/ChromaDB results and BM25 results are combined using reciprocal rank fusion. Plain text, Markdown, PDF, and DOCX are parsed into bounded chunks. Search, model calls, and approved CMD runs stay off the Qt event loop. Searches are local after first-time package/model downloads.

The indexer intentionally handles only extensions listed in settings. Embeddings are written in bounded batches controlled by `models.embeddings.batch_size` (64 by default), and stale vectors are removed only after the replacement chunk records are committed in SQLite. Changing the roots or extensions does not automatically remove old database records; remove the database under `~/.filebot` if you want to rebuild from scratch.

## CMD approval

`config/cmd_policy.yaml` is default-deny. Explicitly blocked patterns take priority. `read_only` and `gated` proposals are written to `cmd_audit` and shown in the popover; the user must click **Approve** before either can run. **Reject** records the decision without running it. The current policy is checked again immediately before execution, and a changed or unreadable policy blocks the command. Every execution records its exit code and bounded output tails. Nanobot's general shell and filesystem tools are disabled; its registered tools are FileBot search, file open, and command proposal only.

Treat policy edits as security-sensitive. Keep installer, privilege, shell chaining, redirection, and broad recursive deletion commands blocked.

## Run unit tests

```powershell
python -m unittest discover -s tests -v
```

The Windows GitHub Actions workflow runs the unit suite on Python 3.11 and 3.12. It installs only the test-time YAML dependency; the full local model, vector database, and desktop UI stack is not needed for these unit tests.

## Data and privacy

The file index, audit log, vectors, and Nanobot workspace stay on this machine. Ollama inference uses the configured local endpoint. Initial dependency, Ollama model, and embedding model downloads require network access. File contents are not sent to a remote model by FileBot's configured Ollama route.

## Current scope

The current release supports the sleep/wake shell, local indexing and hybrid retrieval, compact UI, and gated command flow. Supported document formats are text, Markdown, PDF, and DOCX. Source-code-aware parsing and an installer package are not included yet.

