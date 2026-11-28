"""``FileBot.exe --selfcheck``: verify an installed or frozen build without opening the GUI.

Imports are done lazily so the sleeping entry point never pays for them.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

RUNTIME_MODULES = ("fastembed", "chromadb", "onnxruntime", "fitz", "docx", "watchdog", "yaml",
                   "pynput.keyboard", "PyQt6.QtWidgets",
                   # First-party modules imported lazily at wake time: proves the freeze kept them.
                   "agent.bot_runtime", "retrieval.hybrid_engine", "retrieval.index_worker",
                   "retrieval.parsers", "retrieval.lexical_engine", "agent.demo_llm", "demo_support",
                   "executor.cmd_runner", "ui.popover_card", "ui.result_delegate", "ui.theme")


def _check(report: list[dict[str, Any]], name: str, action: Callable[[], str], *, required: bool = True) -> bool:
    try:
        detail = action()
        report.append({"check": name, "ok": True, "detail": detail})
        return True
    except Exception as exc:  # reporting every failure is the point of this tool
        report.append({"check": name, "ok": False, "required": required, "detail": f"{type(exc).__name__}: {exc}"})
        return not required


def run_selfcheck(settings: dict[str, Any], *, require_assets: bool = False, deep: bool = False,
                  modules: tuple[str, ...] | None = None) -> tuple[bool, list[dict[str, Any]]]:
    """Return ``(all required checks passed, report)``.

    ``require_assets`` makes missing bundled model/engine files a failure (use it
    on the packaged build). ``deep`` also loads the embedding model and starts the
    LLM engine for one tiny request, which needs the real bundled files.
    """
    from resources import app_root, resolve_resource

    report: list[dict[str, Any]] = []
    ok = True
    for module in RUNTIME_MODULES if modules is None else modules:
        ok &= _check(report, f"import {module}", lambda m=module: str(getattr(importlib.import_module(m), "__version__", "ok")))

    def sqlite_fts() -> str:
        with tempfile.TemporaryDirectory() as directory:
            from storage.db import Database
            db = Database(Path(directory) / "check.sqlite3")
            db.initialize()
            if not db.fts_enabled:
                raise RuntimeError("SQLite FTS5 is unavailable")
        return f"sqlite {sqlite3.sqlite_version}, FTS5 ok"

    ok &= _check(report, "database + FTS5", sqlite_fts)
    ok &= _check(report, "command policy", lambda: str(_policy_check()))
    llm = settings["models"]["llm"]
    embeddings = settings["models"]["embeddings"]
    assets = {
        "llama-server": resolve_resource(llm["server_path"]),
        "language model": resolve_resource(llm["model_path"]),
        "embedding model": resolve_resource(embeddings.get("model_dir") or "models/missing"),
    }
    for label, path in assets.items():
        def present(p: Path = path) -> str:
            if p.is_dir():
                if not any(p.glob("*.onnx")):
                    raise FileNotFoundError(f"{p} has no .onnx file")
            elif not p.is_file():
                raise FileNotFoundError(str(p))
            return str(p)
        ok &= _check(report, f"asset: {label}", present, required=require_assets or deep)
    report.append({"check": "app root", "ok": True, "detail": str(app_root())})
    if deep:
        ok &= _check(report, "embedding model loads offline", lambda: _deep_embedding(settings))
        ok &= _check(report, "llama-server answers", lambda: _deep_llm(settings))
    return ok, report


def _policy_check() -> Path:
    from resources import resource_path
    path = resource_path("config", "cmd_policy.yaml")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def _deep_embedding(settings: dict[str, Any]) -> str:
    from fastembed import TextEmbedding
    from resources import resolve_resource
    model = settings["models"]["embeddings"]
    directory = resolve_resource(model["model_dir"])
    embedder = TextEmbedding(model_name=model["model"], specific_model_path=str(directory),
                             local_files_only=True)
    return f"dimension {len(next(iter(embedder.embed(['self check']))))}"


def _deep_llm(settings: dict[str, Any]) -> str:
    from agent.llm_server import LlamaServer
    from resources import resolve_resource
    llm = settings["models"]["llm"]
    server = LlamaServer(resolve_resource(llm["server_path"]), resolve_resource(llm["model_path"]),
                         n_ctx=int(llm.get("n_ctx", 8192)))
    try:
        server.start()
        text = "".join(
            (chunk.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            for chunk in server.chat_stream([{"role": "user", "content": "Reply with the word ok."}], None))
        if not text.strip():
            raise RuntimeError("the model returned no text")
        return f"replied {text.strip()[:40]!r}"
    finally:
        server.stop()


def main(argv: list[str]) -> int:
    """Run from the entry point; writes JSON to ``--selfcheck-out=<file>`` (GUI builds have no console)."""
    from filebot_config import load_settings
    from resources import resource_path, user_settings_path

    out = next((a.split("=", 1)[1] for a in argv if a.startswith("--selfcheck-out=")), None)
    try:
        settings = load_settings(resource_path("config", "settings.yaml"), user_settings_path())
        ok, report = run_selfcheck(settings, require_assets="--require-assets" in argv, deep="--deep" in argv)
    except Exception as exc:
        ok, report = False, [{"check": "settings", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}]
    text = json.dumps({"ok": ok, "report": report}, indent=2)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if ok else 1
