# FileBot handoff #4 — window controls (1.0.0-rc4), 2026-11-28

Goal: add close, minimise and "completely end task" to the GUI. Builds on HANDOFF-3.md (auto mode). Standing rule: every output ships with a HANDOFF-<n>.md.

## What changed
- `ui/popover_card.py`: header buttons `minimize_button` ("–"), `close_button` ("×"), `quit_button` ("Quit", red outline); signals `minimize_requested`, `close_requested`, `quit_requested`; Esc = minimise. Plain-text labels on purpose (no symbol-font dependency on Windows).
- `ui/theme.py`: `QPushButton#winbtn` and `#winquit` styles.
- `daemon/lifecycle_manager.py`: `minimize()` (hide, stay awake, idle timer keeps running), `request_quit()` + `quit_requested` signal; `toggle()` now re-shows a hidden-but-awake popover instead of sleeping; popover signals connected in `_on_wake_ready` (Close -> existing `sleep()`).
- `ui/avatar_bubble.py`: right-click menu "Open / close FileBot" and "Quit FileBot (end task)"; `quit_requested` signal.
- `app.pyw`: `lifecycle.quit_requested` and `avatar.quit_requested` -> `app.quit`; the existing `aboutToQuit` hooks stop watcher, hotkey, popover tasks, llama-server and release the instance lock.
- Docs: README-STEP-BY-STEP.md (panel buttons section), CHANGELOG 1.0.0-rc4, AGENT.md, version 1.0.0-rc4. New `tests/test_window_controls.py` (7 tests).

## Verified (Linux sandbox)
- 126 tests; only the 4 known Linux-only failures remain.
- Offscreen screenshot: header shows status pill, –, ×, Quit without clipping at 440 px width.
- Tests click each button: minimise hides but keeps state and the avatar click restores it; close unloads; Quit emits the app-quit request; Esc minimises.

## NOT verified
- Real process exit on Windows (that `pythonw.exe` and `llama-server.exe` disappear after Quit), the avatar right-click menu appearance, fonts/DPI. `app.quit` was not exercised in a real event loop; the connection is a single signal hookup.
- Quit while an Ask is running waits for the active task to stop (`stop_tasks`), so it can take a moment.

## Next steps (suggested)
1. On Windows: click Quit, then check Task Manager for leftover `pythonw.exe` / `llama-server.exe`. If any remain, send `%USERPROFILE%\.filebot\logs\filebot.log`.
2. Then continue with HANDOFF-3 next steps (semantic search, real model, installer).

## Paste into a new chat (with the zip attached)
> Continue FileBot from HANDOFF-4.md. I'm on Windows and here is what I see: <describe/paste>. Fix it and keep tests green.
