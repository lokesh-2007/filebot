"""On-demand local-LLM runtime: a bundled llama-server plus FileBot's narrow tools."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from agent.fast_router import FastRouter
from agent.llm_server import LlamaServer
from agent.tool_text_bridge import parse_qwen_tool_text
from agent.tools.cmd_proposal_tool import ProposeCmdTool
from agent.tools.file_ops_tool import FileOpsTool
from agent.tools.search_tool import SearchFilesTool
from executor.policy_engine import PolicyEngine
from executor.cmd_runner import CommandRunner
from resources import resolve_resource, resource_path
from storage.db import Database
from retrieval.index_worker import IndexWorker

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 5
MAX_HISTORY_MESSAGES = 8
MAX_TOOL_RESULT_CHARS = 8000
PROMPT_SNIPPET_CHARS = 600

_FILEBOT_SOUL = """You are FileBot, a local Windows file search and CMD proposal assistant.

Answer ordinary questions directly and briefly. Do not pretend to create, edit,
delete, or save files. FileBot has no file-writing tool. Its available tools are
search_files (search the local indexed files), open_file (open an existing
indexed file), and propose_cmd (stage a command for explicit approval in the UI).
Use a tool only when it matches the user's request. Never invent tool calls,
emit JSON that imitates a tool call, or claim an action succeeded unless the
tool actually ran. If a request needs an unavailable action, say so clearly.
All CMD execution requires the user's separate Approve click.
"""

# Tools offered per detected intent. General chat gets none.
_INTENT_TOOLS: dict[str, tuple[str, ...]] = {
    "chat": (),
    "search": ("search_files",),
    "open": ("open_file",),
    "cmd": ("propose_cmd",),
}

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_files": {
        "description": "Search FileBot's local index and return paths and snippets.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "required": ["query"]},
    },
    "open_file": {
        "description": "Open an existing indexed file in its default desktop application.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    },
    "propose_cmd": {
        "description": "Classify and stage a CMD proposal. Never executes a command.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}, "reason": {"type": "string"},
            "cwd": {"type": "string"}}, "required": ["command", "reason"]},
    },
}


def _is_unexecuted_tool_text(content: str) -> bool:
    """Detect model text that imitates a tool invocation instead of using one."""
    value = content.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1]
        if value.endswith("```"):
            value = value[:-3].strip()
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("name") in set(_TOOL_SCHEMAS) \
        and isinstance(payload.get("arguments"), dict)


class _DeltaGate:
    """Forward streamed text, but hold back anything that looks like a tool sketch."""

    def __init__(self, emit: Callable[[str], None] | None, enabled: bool) -> None:
        self.emit, self.enabled = emit, enabled
        self.text = ""
        self.state = "stream" if not enabled else "undecided"

    def feed(self, piece: str) -> None:
        self.text += piece
        if self.emit is None or self.state == "hold":
            return
        if self.state == "undecided":
            stripped = self.text.lstrip()
            if not stripped:
                return
            if stripped.startswith(("{", "```", "<")):
                self.state = "hold"
                return
            self.state = "stream"
            self.emit(self.text)
            return
        self.emit(piece)

    def release(self) -> None:
        """Show held text once we know it was not a tool call."""
        if self.emit is not None and self.state == "hold" and self.text:
            self.state = "stream"
            self.emit(self.text)


class BotRuntime:
    """Own the local LLM only while awake; direct command intent always becomes a proposal."""

    def __init__(self, settings: dict[str, Any], db: Database, engine: Any,
                 server: Any | None = None) -> None:
        self.settings, self.db, self.engine = settings, db, engine
        self.router = FastRouter()
        self.policy = PolicyEngine(resource_path("config", "cmd_policy.yaml"))
        self.runner = CommandRunner(db)
        self.indexer = IndexWorker(settings, db, engine)
        self.server = server
        self._history: list[dict[str, Any]] = []
        self._turn_lock = threading.Lock()
        self._cancel = threading.Event()
        self._turn_active = False
        self._tools: dict[str, Callable[..., str]] | None = None

    # -- lifecycle ---------------------------------------------------------
    def _ensure_server(self) -> Any:
        """Create (once) and start the bundled llama-server on first model use."""
        if self.server is None:
            llm = self.settings["models"]["llm"]
            threads = llm.get("threads")
            self.server = LlamaServer(
                resolve_resource(llm["server_path"]), resolve_resource(llm["model_path"]),
                n_ctx=int(llm.get("n_ctx", 8192)), threads=int(threads) if threads else None,
                log_path=Path(self.db.path).parent / "logs" / "llama-server.log",
            )
        self.server.start()
        return self.server

    def _tool_handlers(self) -> dict[str, Callable[..., str]]:
        if self._tools is None:
            search = SearchFilesTool(self.engine)
            file_ops = FileOpsTool(self.db, roots=self.settings["index"]["roots"])
            proposal = ProposeCmdTool(self.policy, self.db)

            def search_files(query: str, limit: int = 10) -> str:
                return json.dumps(search.execute(query, limit), ensure_ascii=False)

            def open_file(path: str) -> str:
                file_ops.open_file(path)
                return f"Opened {path}"

            def propose_cmd(command: str, reason: str, cwd: str | None = None) -> str:
                return json.dumps(proposal.propose(command, reason, cwd), ensure_ascii=False)

            self._tools = {"search_files": search_files, "open_file": open_file,
                           "propose_cmd": propose_cmd}
        return self._tools

    def cancel_pending(self) -> bool:
        """Abort the active turn. Stops the sidecar so a blocked read returns promptly."""
        if not self._turn_active:
            return False
        self._cancel.set()
        server = self.server
        if server is not None:
            try:
                server.stop()
            except Exception:
                log.debug("Could not stop the LLM engine while cancelling", exc_info=True)
        return True

    def close(self) -> None:
        """Stop the LLM engine and release all references."""
        self._cancel.set()
        server, self.server = self.server, None
        if server is not None:
            try:
                server.stop()
            except Exception:
                log.exception("Could not stop the LLM engine cleanly")
        self._history.clear()
        self._tools = None

    # -- prompt building ---------------------------------------------------
    def _prepare_prompt(self, prompt: str) -> tuple[str, str]:
        """Apply local routing and add only the context needed for one turn."""
        intent = self.router.route(prompt)
        if intent == "search":
            self.indexer.drain(limit=100)
        if intent == "cmd":
            prompt = ("The user is asking for a Windows CMD action. Never execute commands or use any shell. "
                      "If a command is appropriate, call propose_cmd with only the command and a short reason. "
                      "The application requires a separate explicit UI approval before execution. User request: " + prompt)
        if intent == "search":
            hits = [{**hit, "snippet": str(hit.get("snippet", ""))[:PROMPT_SNIPPET_CHARS]}
                    for hit in self.engine.search(prompt, 8)]
            prompt += ("\n\nLocal search results (cite paths; do not invent file contents):\n"
                       + json.dumps(hits, ensure_ascii=False))
        return intent, prompt

    # -- model turn --------------------------------------------------------
    def _stream_once(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     on_delta: Callable[[str], None] | None) -> tuple[str, list[dict[str, Any]]]:
        """Run one model request; return (text, structured tool calls)."""
        server = self._ensure_server()
        gate = _DeltaGate(on_delta, enabled=bool(tools))
        calls: dict[int, dict[str, Any]] = {}
        try:
            for chunk in server.chat_stream(messages, tools or None, cancel=self._cancel):
                if self._cancel.is_set():
                    break
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    gate.feed(str(piece))
                for part in delta.get("tool_calls") or []:
                    slot = calls.setdefault(int(part.get("index", 0)),
                                            {"id": "", "name": "", "arguments": ""})
                    function = part.get("function") or {}
                    slot["id"] = part.get("id") or slot["id"]
                    slot["name"] += function.get("name") or ""
                    slot["arguments"] += function.get("arguments") or ""
        except Exception:
            # Cancelling stops the sidecar, which surfaces here as a broken read.
            if not self._cancel.is_set():
                raise
        if self._cancel.is_set():
            raise RuntimeError("The request was cancelled.")
        ordered = [calls[index] for index in sorted(calls)]
        if not ordered:
            sketch = parse_qwen_tool_text(gate.text, set(t["function"]["name"] for t in tools))
            if sketch is not None:
                ordered = [{"id": "", "name": sketch[0], "arguments": json.dumps(sketch[1])}]
                gate.text = ""
        if not ordered:
            gate.release()
        return gate.text, ordered

    def _execute_tool(self, name: str, raw_arguments: str, allowed: set[str]) -> tuple[str, bool]:
        """Run one allowlisted tool; return (result text for the model, ok)."""
        if name not in allowed:
            return f"Error: tool '{name}' is not available for this request.", False
        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            result = self._tool_handlers()[name](**arguments)
        except Exception as exc:
            log.info("Tool %s failed: %s", name, exc)
            return f"Error: {exc}", False
        return result[:MAX_TOOL_RESULT_CHARS], True

    def _run_turn(self, prompt: str, on_delta: Callable[[str], None] | None) -> str:
        intent, model_prompt = self._prepare_prompt(prompt)
        names = _INTENT_TOOLS.get(intent, ())
        allowed = set(names)
        tools = [{"type": "function", "function": {"name": n, **_TOOL_SCHEMAS[n]}} for n in names]
        with self._turn_lock:
            self._cancel.clear()
            self._turn_active = True
            try:
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": _FILEBOT_SOUL},
                    *self._history, {"role": "user", "content": model_prompt},
                ]
                staged = False
                text = ""
                for _ in range(MAX_TOOL_ITERATIONS):
                    text, calls = self._stream_once(messages, tools, on_delta)
                    if not calls:
                        break
                    messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                        {"id": c["id"] or f"call_{i}", "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                        for i, c in enumerate(calls)]})
                    for i, call in enumerate(calls):
                        result, ok = self._execute_tool(call["name"], call["arguments"], allowed)
                        staged = staged or (ok and call["name"] == "propose_cmd")
                        messages.append({"role": "tool", "tool_call_id": call["id"] or f"call_{i}",
                                         "content": result})
                else:
                    text = "I stopped after too many tool steps. Please try a more specific request."
                answer = self._answer_text(text, staged=staged)
            finally:
                self._turn_active = False
        self._history += [{"role": "user", "content": prompt},
                          {"role": "assistant", "content": answer}]
        del self._history[:-MAX_HISTORY_MESSAGES]
        return answer

    @staticmethod
    def _answer_text(content: str, *, staged: bool = False) -> str:
        """Validate a completed model answer and return safe display text."""
        content = (content or "").strip()
        if _is_unexecuted_tool_text(content):
            return (
                "FileBot did not run that action. I can answer questions, search or open indexed files, "
                "and prepare a CMD proposal for your approval. File creation and editing are not supported."
            )
        if not content:
            if staged:
                return "I staged a command proposal. Review it and click Approve to run it."
            log.warning("The local model returned an empty answer")
            return "The local model returned an empty answer. Please try again."
        return content

    def ask(self, prompt: str) -> str:
        """Handle one user prompt, routing search and CMD intent before the model."""
        return self._run_turn(prompt, None)

    def ask_streaming(self, prompt: str, on_delta: Callable[[str], None]) -> str:
        """Stream answer text to a callback, then return the validated final answer."""
        return self._run_turn(prompt, on_delta)

    def search(self, query: str) -> list[dict[str, Any]]:
        """Return direct local retrieval matches for the compact search view."""
        self.indexer.drain(limit=100)
        return self.engine.search(query, 20)
