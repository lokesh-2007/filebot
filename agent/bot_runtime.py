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
from agent.tools.cmd_proposal_tool import ProposeCmdTool
from agent.tools.file_ops_tool import FileOpsTool
from agent.tools.search_tool import SearchFilesTool
from executor.policy_engine import PolicyEngine
from executor.cmd_runner import CommandRunner
from storage.db import Database
from retrieval.index_worker import IndexWorker

log = logging.getLogger(__name__)


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
        file_ops = FileOpsTool(self.db)
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
        self.bot = Nanobot(loop, config=config)

    def ask(self, prompt: str) -> str:
        """Handle one user prompt, routing search and CMD intent before Nanobot."""
        intent = self.router.route(prompt)
        if intent != "cmd":
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
        # A failed HTTP request may still have loaded Ollama's model. Always
        # request idle unload when this awake session is closed.
        self._model_used = True
        result = self._run_async(self.bot.run(prompt, session_key=self._session, channel="filebot"))
        return str(result.content)

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
        try:
            if not self._model_used:
                return
            from ollama import Client
            ollama = self.settings["models"]["ollama"]
            Client(host=ollama["base_url"]).generate(model=ollama["model"], prompt="", stream=False,
                keep_alive=ollama.get("keep_alive", "0"))
        except Exception:
            log.debug("Ollama idle unload request failed", exc_info=True)
        finally:
            self._model_used = False

