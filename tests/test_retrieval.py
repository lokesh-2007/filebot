"""Tests for hybrid rank fusion without initializing heavyweight dependencies."""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from retrieval.hybrid_engine import HybridEngine


class ReciprocalRankFusionTests(unittest.TestCase):
    def test_shared_high_rank_result_receives_both_votes(self) -> None:
        rows = [
            {"chunk_id": "a", "path": "a.txt", "snippet": "alpha", "page_or_line": None, "heading": None},
            {"chunk_id": "b", "path": "b.txt", "snippet": "beta", "page_or_line": "page 2", "heading": "Beta"},
            {"chunk_id": "c", "path": "c.txt", "snippet": "gamma", "page_or_line": None, "heading": None},
        ]
        results = HybridEngine.reciprocal_rank_fusion(["a", "b", "c"], ["b", "a"], rows, 3)
        self.assertEqual([item["path"] for item in results], ["a.txt", "b.txt", "c.txt"])
        self.assertGreater(results[0]["score"], results[2]["score"])

    def test_limit_and_source_metadata_are_preserved(self) -> None:
        rows = [{"chunk_id": "x", "path": "x.pdf", "snippet": "passage", "page_or_line": "page 4", "heading": "Summary"}]
        result = HybridEngine.reciprocal_rank_fusion(["x"], [], rows, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["page_or_line"], "page 4")
        self.assertEqual(result[0]["heading"], "Summary")


class BoundedEmbeddingTests(unittest.TestCase):
    def test_upsert_embeds_small_batches_and_keeps_old_vectors_until_commit(self) -> None:
        class Vector:
            def tolist(self):
                return [0.25, 0.75]

        class Embedder:
            def __init__(self):
                self.batches = []

            def embed(self, texts):
                self.batches.append(list(texts))
                return [Vector() for _ in texts]

        class Collection:
            def __init__(self):
                self.ids = ["old-vector"]

            def upsert(self, *, ids, **kwargs):
                self.ids.extend(ids)

            def get(self, **kwargs):
                return {"ids": list(self.ids)}

            def delete(self, *, ids):
                self.ids = [item for item in self.ids if item not in ids]

        engine = HybridEngine({"models": {"embeddings": {"batch_size": 2}}}, db=None)
        embedder, collection = Embedder(), Collection()
        engine._embedder, engine._collection = embedder, collection
        chunks = [SimpleNamespace(text=f"chunk {index}") for index in range(5)]
        path = Path("C:/docs/notes.txt")
        engine.upsert(path, chunks, content_hash="digest", refresh_lexical=False)

        self.assertEqual([len(batch) for batch in embedder.batches], [2, 2, 1])
        self.assertIn("old-vector", collection.ids)
        new_ids = [item for item in collection.ids if item != "old-vector"]
        engine.delete_obsolete(path, new_ids)
        self.assertNotIn("old-vector", collection.ids)
        self.assertEqual(len(collection.ids), 5)

    def test_failed_embedding_batch_leaves_committed_vectors_untouched(self) -> None:
        class Vector:
            def tolist(self):
                return [1.0]

        class Embedder:
            def embed(self, texts):
                raise RuntimeError("offline")

        class Collection:
            def __init__(self):
                self.ids = ["committed-old-vector"]

            def get(self, **kwargs):
                return {"ids": list(self.ids)}

            def upsert(self, **kwargs):
                raise AssertionError("a failed embedding must not write a batch")

        engine = HybridEngine({"models": {"embeddings": {"batch_size": 2}}}, db=None)
        collection = Collection()
        engine._embedder, engine._collection = Embedder(), collection
        with self.assertRaisesRegex(RuntimeError, "offline"):
            engine.upsert(Path("C:/docs/notes.txt"), [SimpleNamespace(text="new")],
                          content_hash="new-digest", refresh_lexical=False)
        self.assertEqual(collection.ids, ["committed-old-vector"])


if __name__ == "__main__":
    unittest.main()

