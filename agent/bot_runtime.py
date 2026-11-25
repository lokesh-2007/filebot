"""On-demand HKUDS/nanobot runtime with only FileBot's narrow custom tools."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import json
import logging
import threading
from pathlib import Path
from typing import Any

from agent.fast_router import FastRouter
from agent.ollama_tool_bridge import install_ollama_tool_bridge
from agent.tools.cmd_proposal_tool import ProposeCmdTool
from agent.tools.file_ops_tool import FileOpsTool
from agent.tools.search_tool import SearchFilesTool
from executor.policy_engine import PolicyEngine
from executor.cmd_runner import CommandRunner
from storage.db import Database
from retrieval.index_worker import IndexWorker

log = logging.getLogger(__name__)

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


def _ensure_filebot_instructions(workspace: Path) -> None:
    """Install the default capability instructions without replacing user edits."""
    path = workspace / "SOUL.md"
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(_FILEBOT_SOUL)
    except FileExistsError:
        pass


def _is_unexecuted_tool_text(content: str) -> bool:
    """Detect model text that imitates a tool invocation instead of using one."""
    value = content.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1]
        if value.endswith("```"):
            value = value[:-3].strip()
    try:
        import json
        payload = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("name") in {
        "search_files", "open_file", "propose_cmd"
    } and isinstance(payload.get("arguments"), dict)


class BotRuntime:
    """Own Nanobot only while awake; direct command intent always becomes a proposal."""
    def __init__(self, settings: dict[str, Any], db: Database, engine: Any) -> None:
        self.settings, self.db, self.engine = settings, db, engine
        self.router = FastRouter()
        self.policy = PolicyEngine(Path(__file__).resolve().parents[1] / "config" / "cmd_policy.yaml")
        self.runner = CommandRunner(db)
        self.indexer = IndexWorker(settings, db, engine)
        self.bot = None
        self._model_used = False
        self._session = "filebot:desktop"
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_started = threading.Event()
        self._future_lock = threading.Lock()
        self._active_future: Future[Any] | None = None
        self._turn_lock = threading.Lock()
        self._tool_registries: dict[str, Any] = {}

    def _start_async_loop(self) -> None:
        """Start one persistent event loop for Nanobot's awake lifetime."""
        if self._async_loop is not None and self._async_loop.is_running():
            return
        loop = asyncio.new_event_loop()
        self._async_loop = loop
        self._loop_started.clear()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            self._loop_started.set()
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=run_loop, name="FileBot-Nanobot-asyncio", daemon=True
        )
        self._loop_thread.start()
        if not self._loop_started.wait(timeout=5):
            self._stop_async_loop()
            raise RuntimeError("Nanobot event loop did not start")

    def _run_async(self, coroutine: Any) -> Any:
        """Run a coroutine on Nanobot's persistent loop from a UI worker thread."""
        loop = self._async_loop
        if loop is None or not loop.is_running():
            raise RuntimeError("Nanobot event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        with self._future_lock:
            self._active_future = future
        try:
            return future.result()
        finally:
            with self._future_lock:
                if self._active_future is future:
                    self._active_future = None

    def cancel_pending(self) -> bool:
        """Cancel the active Nanobot turn during an orderly app shutdown."""
        with self._future_lock:
            future = self._active_future
        return bool(future is not None and not future.done() and future.cancel())

    def _stop_async_loop(self) -> None:
        """Stop and close Nanobot's event loop after its resources are released."""
        loop, thread = self._async_loop, self._loop_thread
        self._async_loop = None
        self._loop_thread = None
        if loop is None:
            return
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                log.error("Nanobot event loop thread did not stop cleanly")
                return
        if not loop.is_running() and not loop.is_closed():
            loop.close()

    def _activate(self) -> None:
        if self.bot is not None:
            return
        self._start_async_loop()
        # Heavy agent modules are imported only after the user wakes FileBot.
        from nanobot import Nanobot
        from nanobot.agent.tools.base import Tool, tool_parameters
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.agent.loop import AgentLoop
        from nanobot.config.schema import Config
        from nanobot.config.loader import set_config_path

        settings = self.settings
        data_dir = self.db.path.parent
        workspace = data_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        _ensure_filebot_instructions(workspace)
        model = str(settings["models"]["ollama"]["model"])
        base_url = str(settings["models"]["ollama"].get("api_base", settings["models"]["ollama"]["base_url"]))
        config = Config.model_validate({
            "agents": {"defaults": {"workspace": str(workspace), "model": model, "provider": "ollama",
                                     "max_tool_iterations": 5, "max_concurrent_subagents": 1,
                                     "disabled_skills": ["*"]}},
            "providers": {"ollama": {"api_base": base_url}},
            "tools": {"exec": {"enable": False}, "file": {"enable": False},
                      "web": {"enable": False}, "cli_apps": {"enable": False},
                      "my": {"enable": False},
                      "mcp_servers": {}, "restrict_to_workspace": True},
        })
        # Give Nanobot an instance-local config/data root so sessions, logs, and
        # auxiliary state never fall back to ~/.nanobot.
        nanobot_config = data_dir / "nanobot" / "config.json"
        nanobot_config.parent.mkdir(parents=True, exist_ok=True)
        config.bind_source_path(nanobot_config)
        set_config_path(nanobot_config)
        registry = ToolRegistry()
        search = SearchFilesTool(self.engine)
        file_ops = FileOpsTool(self.db, roots=settings["index"]["roots"])
        proposal = ProposeCmdTool(self.policy, self.db)

        @tool_parameters({"type": "object", "properties": {"query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["query"]})
        class SearchTool(Tool):
            @property
            def name(self) -> str: return "search_files"
            @property
            def description(self) -> str: return "Search FileBot's local index and return paths and snippets."
            @property
            def read_only(self) -> bool: return True
            async def execute(self, query: str, limit: int = 10) -> str:
                return json.dumps(search.execute(query, limit), ensure_ascii=False)

        @tool_parameters({"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
        class OpenTool(Tool):
            @property
            def name(self) -> str: return "open_file"
            @property
            def description(self) -> str: return "Open an existing file in its default desktop application."
            async def execute(self, path: str) -> str:
                file_ops.open_file(path)
                return f"Opened {path}"

        @tool_parameters({"type": "object", "properties": {"command": {"type": "string"},
            "reason": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["command", "reason"]})
        class ProposeTool(Tool):
            @property
            def name(self) -> str: return "propose_cmd"
            @property
            def description(self) -> str: return "Classify and stage a CMD proposal. Never execute a command."
            async def execute(self, command: str, reason: str, cwd: str | None = None) -> str:
                return json.dumps(proposal.propose(command, reason, cwd), ensure_ascii=False)

        registry.register(SearchTool())
        registry.register(OpenTool())
        registry.register(ProposeTool())
        loop = AgentLoop.from_config(config, tool_registry=registry)
        # ToolLoader also discovers Nanobot defaults. Enforce an allowlist after
        # construction so disabled settings or future upstream tools cannot widen access.
        allowed_tools = {"search_files", "open_file", "propose_cmd"}
        for name in tuple(loop.tools.tool_names):
            if name not in allowed_tools:
                loop.tools.unregister(name)
        available_tools = {name: loop.tools.get(name) for name in allowed_tools}
        self._tool_registries = {}
        for intent, names in {
            "chat": set(),
            "search": {"search_files"},
            "open": {"open_file"},
            "cmd": {"propose_cmd"},
        }.items():
            scoped_registry = ToolRegistry()
            for name in sorted(names):
                tool = available_tools.get(name)
                if tool is not None:
                    scoped_registry.register(tool)
            self._tool_registries[intent] = scoped_registry
        install_ollama_tool_bridge(loop.provider)
        self.bot = Nanobot(loop, config=config)

    def _prepare_prompt(self, prompt: str) -> tuple[str, str]:
        """Apply local routing and add only the context needed for one turn."""
        intent = self.router.route(prompt)
        if isinstance(self.settings.get("models"), dict):
            self._check_ollama_ready()
        if intent == "search":
            self.indexer.drain(limit=100)
        self._activate()
        assert self.bot is not None
        if intent == "cmd":
            prompt = ("The user is asking for a Windows CMD action. Never execute commands or use any shell. "
                      "If a command is appropriate, call propose_cmd with only the command and a short reason. "
                      "The application requires a separate explicit UI approval before execution. User request: " + prompt)
        if intent == "search":
            hits = self.engine.search(prompt, 8)
            context = "\n\nLocal search results (cite paths; do not invent file contents):\n" + json.dumps(hits, ensure_ascii=False)
            prompt += context
        return intent, prompt

    def _check_ollama_ready(self) -> None:
        """Fail quickly with recovery steps when Ollama or its model is missing."""
        from urllib.request import urlopen

        ollama = self.settings.get("models", {}).get("ollama", {})
        base_url = str(ollama.get("base_url", "http://127.0.0.1:11434")).rstrip("/")
        model = str(ollama.get("model", "qwen2.5-coder:3b"))
        try:
            with urlopen(f"{base_url}/api/tags", timeout=4) as response:
                payload = json.loads(response.read(1_000_001).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"FileBot cannot reach Ollama at {base_url}. Start Ollama and try again. "
                f"Details: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise RuntimeError(f"Ollama at {base_url} returned an invalid model list.")
        installed = {
            str(item.get("name") or item.get("model"))
            for item in payload["models"] if isinstance(item, dict)
        }
        if model not in installed:
            raise RuntimeError(
                f"FileBot's model '{model}' is not installed in Ollama. Run "
                f"`ollama pull {model}` and try again."
            )

    def _run_agent(self, intent: str, coroutine: Any) -> Any:
        """Run one serialized turn with only tools appropriate for its intent."""
        with self._turn_lock:
            loop = getattr(self.bot, "_loop", None)
            scoped_tools = self._tool_registries.get(intent)
            if loop is None or scoped_tools is None or not hasattr(loop, "tools"):
                return self._run_async(coroutine)
            original_tools = loop.tools
            loop.tools = scoped_tools
            try:
                return self._run_async(coroutine)
            finally:
                loop.tools = original_tools

    @staticmethod
    def _answer_text(result: Any) -> str:
        """Validate a completed Nanobot result and return safe display text."""
        content = str(getattr(result, "content", "") or "").strip()
        if getattr(result, "error", None):
            raise RuntimeError(str(result.error))
        if _is_unexecuted_tool_text(content):
            return (
                "FileBot did not run that action. I can answer questions, search or open indexed files, "
                "and prepare a CMD proposal for your approval. File creation and editing are not supported."
            )
        if not content:
            stop_reason = str(getattr(result, "stop_reason", "") or "")
            log.warning("Nanobot returned an empty answer (stop_reason=%s)", stop_reason)
            return "The local model returned an empty answer. Please try again."
        return content

    def ask(self, prompt: str) -> str:
        """Handle one user prompt, routing search and CMD intent before Nanobot."""
        intent, prompt = self._prepare_prompt(prompt)
        # A failed HTTP request may still have loaded Ollama's model. Always
        # request idle unload when this awake session is closed.
        self._model_used = True
        result = self._run_agent(
            intent, self.bot.run(prompt, session_key=self._session, channel="filebot")
        )
        return self._answer_text(result)

    def ask_streaming(self, prompt: str, on_delta: Any) -> str:
        """Stream answer text to a callback, then return the validated final answer."""
        intent, prompt = self._prepare_prompt(prompt)
        self._model_used = True

        async def collect() -> Any:
            if not callable(getattr(self.bot, "stream", None)):
                result = await self.bot.run(prompt, session_key=self._session, channel="filebot")
                if getattr(result, "content", None):
                    on_delta(str(result.content))
                return result
            result = None
            async for event in self.bot.stream(prompt, session_key=self._session, channel="filebot"):
                if getattr(event, "type", None) == "text.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        on_delta(delta)
                elif getattr(event, "type", None) == "run.completed":
                    result = getattr(event, "result", None)
            return result

        return self._answer_text(self._run_agent(intent, collect()))

    def search(self, query: str) -> list[dict[str, Any]]:
        """Return direct local retrieval matches for the compact search view."""
        self.indexer.drain(limit=100)
        return self.engine.search(query, 20)

    def close(self) -> None:
        """Close Nanobot resources and release all references."""
        try:
            if self.bot is not None:
                try:
                    self._run_async(self.bot.aclose())
                except Exception:
                    log.exception("Could not close Nanobot runtime cleanly")
            self.bot = None
        finally:
            self._stop_async_loop()
        self._tool_registries.clear()
        client = None
        try:
            if not self._model_used:
                return
            from ollama import Client
            ollama = self.settings["models"]["ollama"]
            client = Client(host=ollama["base_url"])
            client.generate(model=ollama["model"], prompt="", stream=False,
                            keep_alive=ollama.get("keep_alive", "0"))
        except Exception:
            log.debug("Ollama idle unload request failed", exc_info=True)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    log.debug("Could not close the Ollama HTTP client cleanly", exc_info=True)
            self._model_used = False
