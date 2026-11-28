"""Auto mode: pick the best retrieval engine and answerer that this machine can actually run.

``runtime.mode`` in settings.yaml (or ``--mode`` / ``FILEBOT_MODE``) is one of:

* ``auto`` (default) - use semantic search when the embedding model and its packages are present,
  otherwise SQLite FTS5 keyword search; use the local LLM when ``llama-server`` and the GGUF are
  present, otherwise answer by quoting the matching files. Nothing is ever downloaded implicitly.
* ``lite``  - always keyword search + extractive answers (no models needed).
* ``full``  - semantic search + local LLM; a missing piece is an error, not a fallback.

The user's own files are indexed in every mode (``--demo`` is the only mode that is sandboxed).
"""
from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from resources import resolve_resource

log = logging.getLogger(__name__)

MODES = ("auto", "lite", "full")


@dataclass(frozen=True)
class Capabilities:
    """What this FileBot instance is really running (shown in the popover header)."""
    semantic: bool
    llm: bool
    demo: bool = False

    @property
    def label(self) -> str:
        if self.demo:
            return "Demo mode"
        if self.semantic and self.llm:
            return "Full mode · AI + semantic search"
        if self.semantic:
            return "Semantic search · quoted answers"
        if self.llm:
            return "Keyword search · AI answers"
        return "Lite mode · keyword search"


def requested_mode(settings: dict[str, Any], argv: list[str] | None = None) -> str:
    """Resolve the mode from --mode=X, FILEBOT_MODE, then settings; invalid values raise."""
    value = None
    for arg in argv or []:
        if arg.startswith("--mode="):
            value = arg.split("=", 1)[1]
    value = value or os.environ.get("FILEBOT_MODE") or settings.get("runtime", {}).get("mode", "auto")
    value = str(value).strip().lower()
    if value not in MODES:
        raise RuntimeError(f"FileBot runtime.mode must be one of {', '.join(MODES)} (got {value!r})")
    return value


def embeddings_ready(settings: dict[str, Any]) -> bool:
    """True when the offline ONNX folder and the fastembed/chromadb packages are all present."""
    model_dir = settings["models"]["embeddings"].get("model_dir")
    if not model_dir:
        return False
    folder = resolve_resource(model_dir)
    if not (folder.is_dir() and any(folder.glob("*.onnx"))):
        return False
    return all(importlib.util.find_spec(name) is not None for name in ("fastembed", "chromadb"))


def llm_ready(settings: dict[str, Any]) -> bool:
    llm = settings["models"]["llm"]
    return resolve_resource(llm["server_path"]).is_file() and resolve_resource(llm["model_path"]).is_file()


def existing_roots(roots: list[str]) -> tuple[list[str], list[str]]:
    """Split configured roots into (usable, missing); expands ``~`` and ``%VAR%``."""
    usable: list[str] = []
    missing: list[str] = []
    for root in roots:
        path = Path(os.path.expandvars(root)).expanduser()
        (usable if path.is_dir() else missing).append(root)
    return usable, missing


def apply_auto_roots(settings: dict[str, Any]) -> list[str]:
    """Make the default ``~/Documents`` root work on OneDrive-redirected PCs; drop missing roots.

    Windows "Known Folder Move" puts Documents under ``%OneDrive%``, so ``~/Documents`` may not
    exist. Returns the roots that were dropped so the caller can log them.
    """
    index = settings["index"]
    roots = list(index["roots"])
    one_drive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if one_drive:
        candidate = str(Path(one_drive) / "Documents")
        known = {os.path.normcase(str(Path(os.path.expandvars(r)).expanduser())) for r in roots}
        if any(r.replace("\\", "/").rstrip("/").lower() in ("~/documents", "%userprofile%/documents")
               for r in roots) and os.path.normcase(candidate) not in known and Path(candidate).is_dir():
            roots.append(candidate)
    usable, missing = existing_roots(roots)
    index["roots"] = usable
    if not usable:
        # Never leave the watcher with nothing to watch: fall back to the home folder's Documents.
        index["roots"] = [str(Path.home() / "Documents")]
    return missing


def build_services(settings: dict[str, Any], db: Any, mode: str) -> tuple[Any, Any, Capabilities]:
    """Create (engine, runtime, capabilities). Caller owns closing both objects."""
    from agent.bot_runtime import BotRuntime
    from agent.demo_llm import DemoServer
    from agent.fast_router import FastRouter
    from retrieval.hybrid_engine import HybridEngine
    from retrieval.lexical_engine import LexicalEngine

    demo = bool(settings.get("runtime", {}).get("demo_mode"))
    want_semantic = (mode == "full") or (mode == "auto" and not demo and embeddings_ready(settings))
    want_llm = (mode == "full") or (mode == "auto" and not demo and llm_ready(settings))

    engine: Any = None
    semantic = False
    if want_semantic:
        try:
            engine = HybridEngine(settings, db)
            engine.activate()
            semantic = True
        except Exception:
            if mode == "full":
                raise
            log.exception("Semantic search could not start; falling back to keyword search")
            if engine is not None:
                try:
                    engine.close()
                except Exception:
                    log.debug("Could not release the failed semantic engine", exc_info=True)
            engine = None
    if engine is None:
        engine = LexicalEngine(settings, db)
        engine.activate()

    runtime: Any = None
    try:
        if want_llm:
            runtime = BotRuntime(settings, db, engine)  # real llama-server, started on first Ask
        else:
            class _QuotingRouter(FastRouter):
                """Without a language model, plain questions are answered from search hits."""
                def route(self, prompt: str) -> str:
                    intent = super().route(prompt)
                    return "search" if intent == "chat" else intent

            runtime = BotRuntime(settings, db, engine, server=DemoServer())
            runtime.router = _QuotingRouter()
    except Exception:
        engine.close()
        raise
    caps = Capabilities(semantic=semantic, llm=want_llm, demo=demo)
    runtime.capabilities = caps
    log.info("FileBot services ready: %s", caps.label)
    return engine, runtime, caps
