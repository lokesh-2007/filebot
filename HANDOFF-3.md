# FileBot handoff #3 — working build on real files (1.0.0-rc3), 2026-11-28

Goal: "original working version" = `python app.pyw` (no `--demo`) works on the user's own folders with zero downloads, and upgrades itself when models are added. Demo mode is unchanged.
Standing user rule: every output ships with a HANDOFF-<n>.md like this one. Older: HANDOFF-2.md (demo front end; HANDOFF.md and PLAN.md were not in the rc2 zip).

## What changed
- `service_factory.py` (new): `runtime.mode` auto|lite|full (also `--mode=` / `FILEBOT_MODE`). `build_services()` returns (engine, runtime, Capabilities). auto = semantic search only if `models/bge-small-en-v1.5-onnx` has an .onnx AND fastembed+chromadb import; LLM only if `bin/llama-server.exe` AND the GGUF exist; otherwise `LexicalEngine` + `DemoServer` (quoted answers). If `HybridEngine.activate()` raises in auto, it falls back to lexical; in `full` it raises. `apply_auto_roots()` drops missing roots and adds `%OneDrive%\Documents` when the default `~/Documents` root is used.
- `daemon/lifecycle_manager.py`: `_WakeWorker.run` now just calls `build_services`.
- `app.pyw`: calls `apply_auto_roots` unless `--demo`; logs roots. `filebot_config.py`: validates `runtime.mode`. `config/settings.yaml`: `runtime.mode: auto`.
- `ui/popover_card.py`: header subtitle shows `runtime.capabilities.label` (e.g. "Lite mode · keyword search").
- Launchers without PowerShell: `Setup.bat`, `FileBot.bat`, `FileBot-Console.bat`. `scripts/setup.ps1`: tries 3.12 then 3.11 and no longer dies on `py -3.11` stderr under PS 5.1.
- Docs: README quick start + mode table, CHANGELOG 1.0.0-rc3, AGENT.md map, version 1.0.0-rc3, `selfcheck.RUNTIME_MODULES` + service_factory. New `tests/test_service_factory.py` (6 tests).

## Verified (Linux sandbox)
- 119 tests; only the 4 known Linux-only failures remain (System32 cwd, `%VAR%` expansion, two command tests).
- Headless end-to-end via `LifecycleManager.wake()` on a temp folder with a missing root: root dropped, popover header "Lite mode · keyword search", search "hackathon" -> 1 result, Ask "when is the hackathon submission due?" -> quoted answer with "28 November".

## NOT verified
- Anything on Windows: `.bat` launchers, the edited `setup.ps1` (no PowerShell here), OneDrive root detection on a real KFM machine, hotkey, fonts.
- Semantic path (fastembed/chromadb) and the real llama-server/GGUF: unchanged code, still untested. Installer (`packaging/build.ps1`) never run.
- Known limits: answers in lite mode are extractive, not generative; a query of 3 chars or fewer must match a whole word. If semantic search is on, then off (fallback) then on again, vectors for files edited during the lite period are stale until those files change again (keyword hits still correct).

## How to run (Windows)
1. Double-click `Setup.bat` (once; needs internet). 2. Double-click `FileBot.bat`. 3. Ctrl+Alt+Space or click the avatar; wait a few seconds for the first scan. Errors: use `FileBot-Console.bat`; logs in `%USERPROFILE%\.filebot\logs\filebot.log`.
Optional upgrades: `python scripts\fetch_models.py` (semantic search), then `bin\llama-server.exe` + `models\qwen2.5-3b-instruct-q4_k_m.gguf` (AI answers). Restart FileBot after adding files.

## Next steps (suggested)
1. Run lite mode on Windows; fix any UI/DPI clipping (`ROW_HEIGHT=92` in `ui/result_delegate.py`).
2. Add the embedding model, confirm semantic search; then llama-server + GGUF, confirm Ask and the tool-call loop.
3. `packaging/build.ps1 -SkipInstaller`, run `dist\FileBot\FileBot.exe`, then the installer.

## Paste into a new chat (with the zip attached)
> Continue FileBot from HANDOFF-3.md. I'm on Windows; I ran `Setup.bat` and `FileBot.bat` and here is what I see: <describe/paste>. Fix it and keep tests green.
