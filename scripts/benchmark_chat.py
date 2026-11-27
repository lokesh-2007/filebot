"""Measure FileBot's first live Ollama chat without saving prompt or output."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from daemon.metrics import process_working_set_mb
from filebot_config import load_settings
from storage.db import Database


class _EmptyEngine:
    """Keep the benchmark independent of the user's indexed files."""

    def search(self, _query: str, _limit: int = 10) -> list[dict[str, Any]]:
        return []


def _rounded(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def main() -> int:
    """Run one fixed, local model request and report timing only."""
    from agent.bot_runtime import BotRuntime

    settings = load_settings(ROOT / "config" / "settings.yaml")
    with tempfile.TemporaryDirectory(prefix="filebot-chat-benchmark-") as directory:
        db = Database(Path(directory) / "state.sqlite3")
        db.initialize()
        runtime = BotRuntime(settings, db, _EmptyEngine())
        started = time.perf_counter()
        first_delta: float | None = None
        streamed_characters = 0

        def receive(delta: str) -> None:
            nonlocal first_delta, streamed_characters
            if delta:
                if first_delta is None:
                    first_delta = time.perf_counter() - started
                streamed_characters += len(delta)

        working_set_before = process_working_set_mb()
        try:
            answer = runtime.ask_streaming(
                "Reply with the single word READY.", receive,
            )
            elapsed = time.perf_counter() - started
            if not isinstance(answer, str) or not answer.strip():
                raise RuntimeError("The local model returned no text answer.")
            report = {
                "status": "ok",
                "model": settings["models"]["ollama"]["model"],
                "seconds_to_first_text": _rounded(first_delta),
                "seconds_to_complete": _rounded(elapsed),
                "streamed_characters": streamed_characters,
                "response_characters": len(answer),
                "working_set_before_mb": _rounded(working_set_before),
                "working_set_after_mb": _rounded(process_working_set_mb()),
                "saved_prompt_or_response": False,
            }
            print(json.dumps(report, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "saved_prompt_or_response": False,
            }, indent=2))
            return 1
        finally:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
