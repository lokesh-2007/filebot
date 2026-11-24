"""On-demand HKUDS/nanobot runtime with only FileBot's narrow custom tools."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from agent.fast_router import FastRouter
from agent.tools.cmd_proposal_tool import ProposeCmdTool
from agent.tools.file_ops_tool import FileOpsTool
from agent.tools.search_tool import SearchFilesTool
from executor.policy_engine import PolicyEngine
from executor.cmd_runner import CommandRunner
from storage.db import Database

log = logging.getLogger(__name__)


class BotRuntime:
    """Own Nanobot only while awake; direct command intent always becomes a proposal."""
    def __init__(self, settings: dict[str, Any], db: Database, engine: Any) -> None:
        self.settings, self.db, self.engine = settings, db, engine
        self.router = FastRouter()
        self.policy = PolicyEngine(Path(__file__).resolve().parents[1] / "config" / "cmd_policy.yaml")
        self.runner = CommandRunner(db)
        self.bot = None
        self._model_used = False
        self._session = "filebot:desktop"

    def _activate(self) -> None:
        if self.bot is not None:
            return
        # Heavy agent modules are imported only after the user wakes FileBot.
        from nanobot import Nanobot
        from nanobot.agent.tools.base import Tool, tool_parameters
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.agent.loop import AgentLoop
        from nanobot.config.schema import Config

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
        result = asyncio.run(self.bot.run(prompt, session_key=self._session, channel="filebot"))
        self._model_used = True
        return str(result.content)

    def search(self, query: str) -> list[dict[str, Any]]:
        """Return direct local retrieval matches for the compact search view."""
        return self.engine.search(query, 20)

    def close(self) -> None:
        """Close Nanobot resources and release all references."""
        if self.bot is not None:
            try:
                asyncio.run(self.bot.aclose())
            except Exception:
                log.exception("Could not close Nanobot runtime cleanly")
        self.bot = None
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
