# FileBot Agent Guide

This file is a quick orientation for coding agents working in this repository. It describes the current implementation; check the code and `README.md` when details may have changed.

## What FileBot does

FileBot is a Windows desktop utility for local file search and local-model chat. A lightweight PyQt6 avatar, global hotkey, SQLite database, and watchdog observer stay available while the assistant sleeps. Retrieval and the local LLM (a bundled llama.cpp `llama-server` sidecar) are loaded after wake. Windows CMD actions are staged for review and can run only after the user approves them in the popover.

Current version: see `filebot_version.py` (the single source of truth) and `CHANGELOG.md`.

## Architecture map

- `app.pyw` — Windows entry point; configures logging, enforces one running instance, and starts UI, watcher, lifecycle, and hotkey components.
- `daemon/lifecycle_manager.py` — wakes and sleeps the assistant, loads/unloads heavy resources, and tracks work so idle shutdown does not interrupt requests.
- `daemon/watcher.py` — watches configured roots and debounces file events into SQLite without loading retrieval models.
- `ui/avatar_bubble.py`, `ui/popover_card.py`, `ui/hotkey_bridge.py` — avatar, search/chat/approval popover, and global hotkey bridge.
- `storage/db.py` — SQLite WAL storage for indexed files/chunks, dirty work, file signals/glossary, and command audit state.
- `retrieval/parsers.py` — bounded plain-text/code, HTML, PDF, DOCX, XLSX, and PPTX parsing (`SUPPORTED_EXTENSIONS`).
- `retrieval/index_worker.py` — drains durable file changes, hashes and parses files, retries failures, and refreshes the index.
- `retrieval/hybrid_engine.py` — lazy FastEmbed/ONNX embeddings, ChromaDB, SQLite-FTS5 lexical search (via `Database.search_lexical`), and reciprocal-rank fusion with per-file dedupe.
- `agent/bot_runtime.py` — lazy local-LLM runtime: fast intent routing, a bounded (≤5 step) tool loop, and the narrow per-intent tool allowlist.
- `agent/llm_server.py` — owns the loopback-only `llama-server` child process (free port, health poll, log file, kill-on-close job on Windows) and streams `/v1/chat/completions`.
- `agent/tool_text_bridge.py` — parses small models' plain-text JSON tool sketches into structured calls.
- `resources.py` — frozen-aware locations for bundled read-only files (`config/`, `models/`, `bin/`) and the per-user override file (`%APPDATA%\FileBot\settings.yaml`, merged by `load_settings(path, user_path)`).
- `selfcheck.py` — `FileBot.exe --selfcheck`: import/DB/asset report used by CI and `packaging/build.ps1`.
- `packaging/` — `filebot.spec` (PyInstaller one-folder; models are staged afterwards, not frozen), `build.ps1`, `filebot.iss` (Inno Setup).
- `agent/tools/` — `search_files`, safe opening of indexed files, and command proposals.
- `executor/policy_engine.py`, `executor/cmd_runner.py` — default-deny command classification and the sole approved-command execution path.
- `filebot_config.py`, `config/settings.yaml` — settings validation, defaults, and Windows-friendly path expansion.
- `config/cmd_policy.yaml` — blocked/read-only/gated command patterns. Treat policy edits as security-sensitive.
- `scripts/setup.ps1`, `scripts/start.ps1` — environment setup and desktop-session launch.
- `tests/` — unit, Qt lifecycle, retrieval, watcher, startup, tool-bridge, and runtime checks. `tests/fake_llama_server.py` stands in for the real engine so runtime tests need no model.

## Safety and privacy invariants

1. Do not import FastEmbed or ChromaDB from the sleeping entry-point path. Keep those imports behind wake/activation code; `tests/test_startup.py` checks this.
2. Index and open only files contained within configured roots (`filebot_config.IndexScope` resolves roots once and applies the exclusion rules). Resolve paths before checking containment; fail closed for invalid paths and symlinks escaping a root.
3. Keep parsing and indexing bounded by the configured file, extracted-text, DOCX archive, chunk, and embedding-batch limits. Preserve dirty-queue retries when indexing fails or a file changes mid-read.
4. Do not add shell, filesystem-write, web, or other broad tools for the model; `_execute_tool` refuses anything outside the intent's allowlist. FileBot's agent tools are restricted per turn: general chat has none, search turns get local search, open requests get safe file opening, and CMD requests get proposal only.
5. Never execute a proposal from the model response. `propose_cmd` only classifies and stages an audit row. The user must explicitly approve in the UI; `CommandRunner` rechecks current policy and claims an approved row exactly once.
6. Keep `config/cmd_policy.yaml` default-deny. Block shell chaining, redirection, expansion, installer/privilege/disk operations, and any command not deliberately allowlisted. A `read_only` classification still goes through the visible approval flow.
7. Keep inference on the bundled `llama-server`, bound to 127.0.0.1 only. Network is only expected for initial dependency downloads and `scripts/fetch_models.py` unless the user changes this behavior.

## Development workflow

- Target Windows 10/11 and 64-bit Python 3.11 or 3.12.
- Read `README.md`, the relevant module, and nearby tests before changing behavior. Prefer narrow changes with focused regression coverage.
- Use a dedicated `.venv`; install with `python -m pip install -r requirements.txt` or `scripts/setup.ps1`.
- Run the full suite with `python -m unittest discover -s tests -v`. Also run `python -m compileall -q .`, `python -m pip check`, and `git diff --check` when dependencies or release code change.
- Stream model text through `BotRuntime.ask_streaming` for chat UI work; send UI updates through Qt signals and validate the final result after the stream completes.
- Do not claim live inference was verified unless a real local `llama-server` request with a real GGUF model succeeded. The unit suite uses `tests/fake_llama_server.py` and needs no model assets.
- Keep user-facing configuration documented in `README.md`; record release changes and verification in `CHANGELOG.md`; update `filebot_version.py` and the README heading together for each version.
- Use one local commit per coherent version/update, not one commit per file. Push to the configured GitHub remote only when the user asks.

## Known release scope

- The repository currently provides a source-based Windows setup and launcher, not a signed installer.
- First-use downloads require network access: Python dependencies and `python scripts/fetch_models.py` (embedding model; without it FastEmbed downloads on first wake). `llama-server.exe` and the GGUF model must be placed by hand in source checkouts (see README, "Local model").
- Supported indexed formats are listed in `retrieval/parsers.py` (`SUPPORTED_EXTENSIONS`); enabling them is opt-in through `index.include_extensions`. Filename-only indexing of unparsed types is not implemented.
- The lexical index needs SQLite FTS5 (bundled with Python's sqlite3 on Windows); without it search falls back to a slower LIKE scan.
- Check the current changelog before assuming a limitation is still present; this project is actively being hardened.
- Packaging rules: never write into the app folder at runtime (use `user_config_dir()` / `~/.filebot`); resolve bundled files with `resource_path`/`resolve_resource`, never `__file__`; any new lazily imported first-party package must be added to the `collect_submodules` list in `packaging/filebot.spec` and to `selfcheck.RUNTIME_MODULES`.
- The installer/PyInstaller flow was only exercised on Linux (a freeze plus `--selfcheck`); Windows behavior, Inno Setup and installer size limits are unverified.
