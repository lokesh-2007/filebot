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

The FastEmbed embedding model is downloaded on the first wake into the cache directory in `config/settings.yaml`. ChromaDB data and the SQLite database are stored under `~/.filebot` by default.

Edit `config/settings.yaml` to choose folders to index, supported extensions, maximum file size, model, Ollama URL, and `keep_alive`. Set `keep_alive` to `"0"` for immediate Ollama unload or `"2m"` to allow a two-minute model cache. The avatar hotkey defaults to **Ctrl+Alt+Space**. The initial watched roots default to `~/Documents`.

Start FileBot with:

```powershell
pythonw .\app.pyw
```

For startup diagnostics, use `python .\app.pyw` from a console. `pynput` may require granting accessibility permissions or running in the signed-in user's desktop session.

## Search and indexing

Watchdog records debounced file changes in SQLite. It does not load an embedding model. On wake, the index worker drains this durable queue and combines dense FastEmbed/ChromaDB results with BM25 results using reciprocal rank fusion. Plain text, Markdown, PDF, and DOCX are parsed into bounded chunks. Searches are local after first-time package/model downloads.

The indexer intentionally handles only extensions listed in settings. Changing the roots or extensions does not automatically remove old database records; remove the database under `~/.filebot` if you want to rebuild from scratch.

## CMD approval

`config/cmd_policy.yaml` is default-deny. Explicitly blocked patterns take priority. A `read_only` match can be handled without a confirmation; `gated` commands are written to `cmd_audit` and shown in the popover. FileBot executes a gated command only after the user clicks **Approve**. **Reject** records the decision without running it. Every execution records its exit code and bounded output tails. Nanobot's general shell and filesystem tools are disabled; its registered tools are FileBot search, file open, and command proposal only.

Treat policy edits as security-sensitive. Keep installer, privilege, shell chaining, redirection, and broad recursive deletion commands blocked.

## Data and privacy

The file index, audit log, vectors, and Nanobot workspace stay on this machine. Ollama inference uses the configured local endpoint. Initial dependency, Ollama model, and embedding model downloads require network access. File contents are not sent to a remote model by FileBot's configured Ollama route.

## Current scope

The first implementation supports the core sleep/wake shell, local indexing/retrieval, compact UI, and gated command flow. The tree-sitter package is included for a future structured source-code parser; current parsers cover text, Markdown, PDF, and DOCX. FileBot currently drains queued index work during wake rather than running a second always-on index worker.
