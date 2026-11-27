# FileBot Agent Guide

This file is a quick orientation for coding agents working in this repository. It describes the current implementation; check the code and `README.md` when details may have changed.

## What FileBot does

FileBot is a Windows desktop utility for local file search, safe file actions, and local-model chat. A resizable PyQt6 chat window, system tray, global hotkey, SQLite database, and watchdog observer stay available while heavyweight retrieval and the Nanobot agent sleep. CMD and allowlisted Windows PowerShell actions are staged for review and can run only after the user approves them in the chat window.

Current version: see `filebot_version.py` (the single source of truth) and `CHANGELOG.md`.

## Architecture map

- `app.pyw` — Windows entry point; configures logging, enforces one running instance, and starts UI, watcher, lifecycle, and hotkey components.
- `daemon/lifecycle_manager.py`, `daemon/metrics.py` — wake/sleep supervision and privacy-safe startup, response latency, and Windows working-set diagnostics.
- `daemon/watcher.py` — watches configured roots and debounces file events into SQLite without loading retrieval models.
- `ui/popover_card.py`, `ui/hotkey_bridge.py` — resizable single-prompt chat window and global hotkey bridge. `ui/avatar_bubble.py` is retained for its visual tests but is no longer the app entry point.
- `storage/db.py` — SQLite WAL storage for indexed files/chunks, dirty work, file signals/glossary, and command audit state.
- `retrieval/parsers.py` — bounded TXT/Markdown, PDF, DOCX, and offline-checked Tree-sitter source parsing.
- `retrieval/index_worker.py` — drains durable file changes, hashes and parses files, retries failures, and refreshes the index.
- `retrieval/hybrid_engine.py` — lazy FastEmbed/ONNX embeddings, ChromaDB, BM25, and reciprocal-rank fusion.
- `agent/bot_runtime.py` — lazy Nanobot/Ollama runtime, fast intent routing, and the narrow FileBot tool registry.
- `agent/tools/` — `search_files`, bounded listing/reading/opening, root-scoped new-text-file creation, staged Recycle Bin deletes, and command proposals.
- `executor/policy_engine.py`, `executor/cmd_runner.py` — default-deny command classification and the sole approved-command execution path.
- `filebot_config.py`, `config/settings.yaml` — settings validation, defaults, and Windows-friendly path expansion.
- `config/cmd_policy.yaml` — default-deny CMD and PowerShell blocked/read-only/gated patterns. Treat policy edits as security-sensitive.
- `scripts/setup.ps1`, `scripts/start.ps1` — environment setup and desktop-session launch.
- `tests/` — unit, Qt lifecycle, retrieval, watcher, startup, and Ollama tool-bridge checks.
- `rust/` — in-progress native migration; see `rust/README.md`. Do not treat the core crate as a desktop replacement until its parity gates pass.
- `ROADMAP.md` — acceptance status against the attached FileBot architecture and research plan.
- Rust core modules: `rust/src/config.rs`, `rust/src/storage/`, `rust/src/daemon/watcher.rs`, `rust/src/executor/`, and `rust/src/agent/`.

## Safety and privacy invariants

1. Do not import FastEmbed, BM25, ChromaDB, or Nanobot from the sleeping entry-point path. Keep those imports behind wake/activation code; `tests/test_startup.py` checks this.
2. Index and open only files contained within configured roots. Resolve paths before checking containment; fail closed for invalid paths and symlinks escaping a root.
3. Keep parsing and indexing bounded by the configured file, extracted-text, DOCX archive, chunk, and embedding-batch limits. Preserve dirty-queue retries when indexing fails or a file changes mid-read.
4. Do not add Nanobot shell, broad filesystem, or web tools. FileBot's per-intent tools are restricted: chat gets bounded indexed search/read and configured-root listing; create turns get only the root-scoped no-overwrite text creator plus read/search context; delete turns stage one exact configured-root file and require a one-time confirmation token before Recycle Bin; CMD and PowerShell turns get proposal-only tools.
5. Never execute a proposal from the model response. `propose_cmd` and `propose_powershell` only classify and stage an audit row. The user must explicitly approve in the UI; `CommandRunner` rechecks the shell-specific current policy and claims an approved row exactly once.
6. Keep `config/cmd_policy.yaml` default-deny. Block shell chaining, redirection, expansion, installer/privilege/disk operations, and any command not deliberately allowlisted. A `read_only` classification still goes through the visible approval flow.
7. Keep inference on the configured local Ollama endpoint. Network is only expected for initial dependency/model downloads unless the user changes this behavior.

## Development workflow

- Target Windows 10/11 and 64-bit Python 3.11 or 3.12.
- Read `README.md`, the relevant module, and nearby tests before changing behavior. Prefer narrow changes with focused regression coverage.
- Use a dedicated `.venv`; install with `python -m pip install -r requirements.txt` or `scripts/setup.ps1`.
- Run the full suite with `python -m unittest discover -s tests -v`. Also run `python -m compileall -q .`, `python -m pip check`, and `git diff --check` when dependencies or release code change.
- Use Nanobot's streaming API for chat UI work; send UI updates through Qt signals and validate the final result after the stream completes.
- Do not claim live inference was verified unless a real local Ollama request succeeded. The unit suite does not require Ollama or downloaded model assets.
- Keep user-facing configuration documented in `README.md`; record release changes and verification in `CHANGELOG.md`; update `filebot_version.py` and the README heading together for each version.
- Use one local commit per coherent version/update, not one commit per file. Push to the configured GitHub remote only when the user asks.

## Known release scope

- The repository currently provides a source-based Windows setup and launcher, not a signed installer.
- First-use downloads require network access: Python dependencies, the configured Ollama model (`ollama pull qwen2.5-coder:3b` by default), and the FastEmbed model.
- Supported indexed formats include bounded TXT/Markdown, PDF/DOCX, and syntax-aware source chunks for cached Tree-sitter grammars. Setup attempts to cache grammars; if GitHub is unavailable it warns and bounded plain-text chunking remains available. Indexing never downloads them.
- Check the current changelog before assuming a limitation is still present; this project is actively being hardened.
