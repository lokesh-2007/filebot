"""Offline embedding loading and embedding-model change handling (fake fastembed/chromadb)."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from retrieval.hybrid_engine import HybridEngine
from storage.db import Database

HERE = Path(__file__).resolve().parent


class _Collection:
    def __init__(self, metadata) -> None:
        self.metadata = metadata


class _Client:
    store: dict[str, _Collection] = {}
    settings = None

    def __init__(self, path, settings=None) -> None:
        type(self).settings = settings

    def get_collection(self, name):
        if name not in self.store:
            raise ValueError("missing")
        return self.store[name]

    def delete_collection(self, name):
        del self.store[name]

    def create_collection(self, name, metadata):
        self.store[name] = _Collection(metadata)
        return self.store[name]


def _fake_modules(dim: int, captured: dict):
    class TextEmbedding:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def embed(self, texts):
            return iter([[0.0] * dim for _ in texts])

    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = TextEmbedding
    chroma = types.ModuleType("chromadb")
    chroma.PersistentClient = _Client
    config = types.ModuleType("chromadb.config")
    config.Settings = lambda **kw: kw
    chroma.config = config
    return {"fastembed": fastembed, "chromadb": chroma, "chromadb.config": config}


class OfflineEmbeddingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = Database(self.dir / "fb.sqlite3")
        self.db.initialize()
        _Client.store = {}
        self.model_dir = self.dir / "onnx"

    def settings(self, **embeddings):
        emb = {"model": "BAAI/bge-small-en-v1.5", "cache_dir": str(self.dir / "cache"),
               "model_dir": str(self.model_dir), **embeddings}
        return {"index": {}, "models": {"embeddings": emb,
                                        "vector_store": {"path": str(self.dir / "vec")}}}

    def activate(self, dim=384, **embeddings):
        captured: dict = {}
        with patch.dict(sys.modules, _fake_modules(dim, captured)):
            engine = HybridEngine(self.settings(**embeddings), self.db)
            engine.activate()
        return engine, captured

    def test_bundled_model_dir_loads_offline_and_disables_telemetry(self) -> None:
        self.model_dir.mkdir()
        (self.model_dir / "model_optimized.onnx").write_bytes(b"x")
        _, captured = self.activate()
        self.assertEqual(captured["specific_model_path"], str(self.model_dir))
        self.assertTrue(captured["local_files_only"])
        self.assertEqual(_Client.settings, {"anonymized_telemetry": False})

    def test_missing_or_empty_model_dir_falls_back_to_download_cache(self) -> None:
        _, captured = self.activate()
        self.assertNotIn("specific_model_path", captured)
        self.model_dir.mkdir()  # exists but empty: must not be treated as a bundle
        _, captured = self.activate()
        self.assertNotIn("specific_model_path", captured)

    def test_collection_is_stamped_and_reused_when_model_is_unchanged(self) -> None:
        engine, _ = self.activate()
        meta = _Client.store["filebot_chunks"].metadata
        self.assertEqual((meta["embedding_model"], meta["embedding_dim"]), ("BAAI/bge-small-en-v1.5", 384))
        first = _Client.store["filebot_chunks"]
        self.activate()
        self.assertIs(_Client.store["filebot_chunks"], first)

    def test_model_or_dimension_change_rebuilds_vectors_and_requeues_files(self) -> None:
        doc = self.dir / "a.txt"
        doc.write_text("hello", encoding="utf-8")
        self.db.replace_file(doc, content_hash="abc", size=5, mtime=1.0,
                             chunks=[{"text": "hello", "page_or_line": None, "heading": None}])
        self.activate()
        first = _Client.store["filebot_chunks"]
        self.db.remove_dirty(doc, 1e18)  # clear anything queued by the first activation
        self.activate(dim=768)
        self.assertIsNot(_Client.store["filebot_chunks"], first)
        self.assertEqual(_Client.store["filebot_chunks"].metadata["embedding_dim"], 768)
        self.assertEqual(self.db.get_file(doc)["content_hash"], "")
        self.assertEqual([r["path"] for r in self.db.pending_dirty()], [str(doc.resolve())])

    def test_legacy_unstamped_collection_is_rebuilt_once(self) -> None:
        _Client.store["filebot_chunks"] = _Collection({"hnsw:space": "cosine"})
        self.activate()
        self.assertEqual(_Client.store["filebot_chunks"].metadata["embedding_dim"], 384)


if __name__ == "__main__":
    unittest.main()
