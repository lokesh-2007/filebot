"""Measure isolated FileBot wake, indexing, cold/warm chat, and memory peaks.

The benchmark uses a temporary database, vector store, and synthetic corpus.
It never reads or records user files, prompts, or model responses. Embedding
downloads are disabled unless ``--allow-model-download`` is passed.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from daemon.metrics import process_memory_snapshot_mib, system_memory_snapshot_mib
from filebot_config import expand_config_path, load_settings
from storage.db import Database


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _json_request(url: str, *, payload: dict[str, Any] | None = None,
                  timeout: float = 0.5) -> dict[str, Any] | None:
    try:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read(1_000_001).decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeDecodeError, URLError):
        return None


def _ollama_loaded_memory(base_url: str) -> dict[str, float] | None:
    """Read Ollama's loaded-model and GPU allocation estimates from /api/ps."""
    payload = _json_request(f"{base_url.rstrip('/')}/api/ps")
    models = payload.get("models") if payload else None
    if not isinstance(models, list):
        return None
    def memory_bytes(item: dict[str, Any], key: str) -> int:
        try:
            return max(0, int(item.get(key, 0)))
        except (TypeError, ValueError):
            return 0
    return {
        "loaded_model_count": float(len(models)),
        "loaded_model_size_mib": sum(
            memory_bytes(item, "size") for item in models if isinstance(item, dict)
        ) / (1024 * 1024),
        "loaded_model_vram_mib": sum(
            memory_bytes(item, "size_vram") for item in models if isinstance(item, dict)
        ) / (1024 * 1024),
    }


def _model_quantization(base_url: str, model: str) -> str | None:
    """Return Ollama's model quantization metadata when its API is reachable."""
    payload = _json_request(f"{base_url.rstrip('/')}/api/show", payload={"model": model}, timeout=2)
    details = payload.get("details") if payload else None
    value = details.get("quantization_level") if isinstance(details, dict) else None
    return str(value) if value else None


class _MemorySampler:
    """Sample FileBot/Ollama process, model, and whole-system memory peaks."""

    def __init__(self, base_url: str, interval: float = 0.5) -> None:
        self.base_url, self.interval = base_url, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._process_samples: list[dict[str, float]] = []
        self._system_samples: list[dict[str, float]] = []
        self._model_samples: list[dict[str, float]] = []

    def _sample_once(self) -> None:
        process = process_memory_snapshot_mib()
        system = system_memory_snapshot_mib()
        model = _ollama_loaded_memory(self.base_url)
        with self._lock:
            if process is not None:
                self._process_samples.append(process)
            if system is not None:
                self._system_samples.append(system)
            if model is not None:
                self._model_samples.append(model)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._sample_once()
        self._thread = threading.Thread(target=self._run, name="FileBotMemoryBenchmark", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._sample_once()

    def report(self) -> dict[str, Any]:
        with self._lock:
            process = list(self._process_samples)
            system = list(self._system_samples)
            models = list(self._model_samples)
        process_peak: dict[str, float] = {}
        if process:
            for key in process[0]:
                process_peak[key] = max(item.get(key, 0.0) for item in process)
        model_peak: dict[str, float] = {}
        if models:
            for key in models[0]:
                model_peak[key] = max(item.get(key, 0.0) for item in models)
        total = system[0].get("physical_total_mib") if system else None
        minimum_available = min(
            (item.get("physical_available_mib", float("inf")) for item in system),
            default=None,
        )
        # Per-process working sets can double-count shared pages. Private commit
        # is reported separately and is the safer managed-process budget signal.
        process_peak["chatbot_private_commit_mib"] = max((
            item.get("filebot_private_commit_mib", 0.0)
            + item.get("ollama_private_commit_mib", 0.0) for item in process
        ), default=0.0)
        process_peak["chatbot_working_set_sum_mib"] = max((
            item.get("filebot_working_set_mib", 0.0)
            + item.get("ollama_working_set_mib", 0.0) for item in process
        ), default=0.0)
        return {
            "sample_count": max(len(process), len(system), len(models)),
            "process_peak_mib": process_peak or None,
            "ollama_model_allocation_peak_mib": model_peak or None,
            "system_physical_total_mib": _rounded(total),
            "system_physical_available_min_mib": _rounded(minimum_available),
            "system_physical_used_peak_mib": _rounded(
                total - minimum_available if total is not None and minimum_available is not None else None
            ),
            "working_set_sum_note": "May double-count shared pages across processes.",
            "gpu_note": "Ollama size_vram is a model allocation estimate, not total GPU or shared-memory use.",
        }


def _embedding_cache_exists(settings: dict[str, Any]) -> bool:
    cache = expand_config_path(settings["models"]["embeddings"]["cache_dir"])
    if not cache.exists():
        return False
    try:
        return any(path.is_file() and path.suffix.casefold() == ".onnx"
                   for path in cache.rglob("*.onnx"))
    except OSError:
        return False


def _write_corpus(root: Path, file_count: int, file_size_kb: int) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    body = "Synthetic FileBot benchmark corpus for local indexing and retrieval. "
    content = (body * ((file_size_kb * 1024 // len(body)) + 1))[:file_size_kb * 1024]
    paths: list[Path] = []
    for index in range(file_count):
        path = root / f"benchmark-{index:05d}.txt"
        path.write_text(f"Synthetic document {index}.\n{content}", encoding="utf-8")
        paths.append(path)
    return paths


def _measure_turn(runtime: Any) -> dict[str, Any]:
    started = time.perf_counter()
    first_text: float | None = None
    streamed_characters = 0

    def receive(delta: str) -> None:
        nonlocal first_text, streamed_characters
        if delta:
            if first_text is None:
                first_text = time.perf_counter() - started
            streamed_characters += len(delta)

    try:
        answer = runtime.ask_streaming("Reply with the single word READY.", receive)
        return {
            "status": "ok",
            "seconds_to_first_text": _rounded(first_text),
            "seconds_to_complete": _rounded(time.perf_counter() - started),
            "response_characters": len(answer) if isinstance(answer, str) else 0,
            "streamed_characters": streamed_characters,
            "timings": getattr(runtime, "_last_request_timings", {}),
        }
    except Exception as exc:
        return {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-files", type=int, default=100)
    parser.add_argument("--index-file-kb", type=int, default=16)
    parser.add_argument("--allow-model-download", action="store_true",
                        help="Allow FastEmbed to download its model when cache is missing.")
    args = parser.parse_args()
    if not 1 <= args.index_files <= 10_000 or not 1 <= args.index_file_kb <= 1024:
        parser.error("index corpus size is outside safe benchmark bounds")

    from agent.bot_runtime import BotRuntime
    settings = copy.deepcopy(load_settings(ROOT / "config" / "settings.yaml"))
    base_url = str(settings["models"]["ollama"]["base_url"])
    model_name = str(settings["models"]["ollama"]["model"])
    model_quantization = _model_quantization(base_url, model_name)

    with tempfile.TemporaryDirectory(prefix="filebot-product-benchmark-") as directory:
        sandbox = Path(directory)
        corpus = sandbox / "corpus"
        files = _write_corpus(corpus, args.index_files, args.index_file_kb)
        settings["index"]["roots"] = [str(corpus)]
        settings["models"]["vector_store"]["path"] = str(sandbox / "vectors")
        db = Database(sandbox / "state.sqlite3")
        db.initialize()
        from retrieval.hybrid_engine import HybridEngine
        from retrieval.index_worker import IndexWorker

        sampler = _MemorySampler(base_url)
        sampler.start()
        engine = runtime = None
        report: dict[str, Any] = {
            "model": model_name,
            "model_quantization": model_quantization,
            "context_tokens": settings["models"]["ollama"].get("num_ctx"),
            "hardware": {"machine": platform.machine(), "processor": platform.processor()},
            "corpus": {"files": len(files), "file_size_kb": args.index_file_kb,
                       "synthetic_only": True},
            "saved_prompt_or_response": False,
        }
        try:
            embedding_cache_ready = _embedding_cache_exists(settings)
            if not args.allow_model_download and not embedding_cache_ready:
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            engine = HybridEngine(settings, db)
            wake_started = time.perf_counter()
            engine.activate()
            report["wake_seconds"] = _rounded(time.perf_counter() - wake_started)
            runtime = BotRuntime(settings, db, engine)
            if not args.allow_model_download and not embedding_cache_ready:
                report["embedding_indexing"] = {
                    "status": "skipped",
                    "reason": "FastEmbed model is not cached; use --allow-model-download to opt in.",
                }
            else:
                if engine._embedder is None:
                    report["embedding_indexing"] = {
                        "status": "skipped", "reason": "Embedding runtime unavailable offline.",
                    }
                else:
                    db.enqueue_dirty_many([(path, "upsert") for path in files])
                    index_started = time.perf_counter()
                    processed = IndexWorker(settings, db, engine).drain(limit=len(files))
                    report["embedding_indexing"] = {
                        "status": "ok" if processed == len(files) else "partial",
                        "seconds": _rounded(time.perf_counter() - index_started),
                        "files_processed": processed,
                    }
                report["cold_chat"] = _measure_turn(runtime)
                report["warm_chat"] = _measure_turn(runtime)
        except Exception as exc:
            report["benchmark_error"] = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            model_before_cleanup = _ollama_loaded_memory(base_url)
            cleanup_started = time.perf_counter()
            for item in (runtime, engine):
                if item is not None:
                    try:
                        item.close()
                    except Exception as exc:
                        report.setdefault("cleanup_errors", []).append(str(exc))
            cleanup_seconds = _rounded(time.perf_counter() - cleanup_started)
            model_after_cleanup = _ollama_loaded_memory(base_url)
            before_count = model_before_cleanup.get("loaded_model_count", 0) if model_before_cleanup else 0
            after_count = model_after_cleanup.get("loaded_model_count", 0) if model_after_cleanup else 0
            unload_confirmed = before_count > 0 and after_count == 0
            report["idle_model_unload_confirmed"] = unload_confirmed
            report["idle_unload_seconds" if unload_confirmed else "cleanup_seconds"] = cleanup_seconds
            sampler.stop()
            report["memory"] = sampler.report()
            print(json.dumps(report, indent=2))
    return 0 if report.get("cold_chat", {}).get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
