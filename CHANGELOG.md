# Changelog

Version numbers follow semantic versioning. This project is pre-1.0; minor
versions may still change behavior while the Windows release is stabilized.

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
