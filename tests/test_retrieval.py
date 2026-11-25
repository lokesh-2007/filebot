"""Tests for hybrid rank fusion without initializing heavyweight dependencies."""
from __future__ import annotations

import unittest
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

from retrieval.hybrid_engine import HybridEngine
from retrieval.parsers import parse_file

ROOT = Path(__file__).resolve().parents[1]


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


class ParserResourceLimitTests(unittest.TestCase):
    def test_text_parser_caps_extracted_characters(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "large.txt"
            path.write_text("x" * 1000, encoding="utf-8")

            chunks = parse_file(path, chunk_chars=100, overlap=0, max_chars=250)

            self.assertLessEqual(sum(len(chunk.text) for chunk in chunks), 250)

    def test_docx_parser_rejects_archive_expansion_over_limit(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "large.docx"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", "x" * 1000)

            with self.assertRaisesRegex(ValueError, "DOCX expands"):
                parse_file(path, max_docx_uncompressed_bytes=100)


class BoundedEmbeddingTests(unittest.TestCase):
    def test_lexical_refresh_hides_stale_rows_outside_current_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            allowed = base / "allowed"
            outside = base / "outside"
            allowed.mkdir()
            outside.mkdir()

            def row(path: Path, chunk_id: str) -> dict[str, object]:
                return {"chunk_id": chunk_id, "path": str(path), "file_name": path.name,
                        "snippet": "matching content", "page_or_line": None, "heading": None}

            class FakeDatabase:
                def all_chunks(self):
                    return [row(allowed / "in.txt", "inside"), row(outside / "old.txt", "outside")]

            engine = HybridEngine({"index": {"roots": [str(allowed)]}}, FakeDatabase())
            engine._refresh_bm25()

            self.assertEqual([item["chunk_id"] for item in engine._rows], ["inside"])

    def test_short_identifier_search_returns_exact_hits_only(self) -> None:
        engine = HybridEngine({}, db=None)
        engine._rows = [
            {"file_name": "guide.md", "path": "guide.md", "snippet": "Use an h1 heading here.",
             "page_or_line": None, "heading": None},
            {"file_name": "readme.md", "path": "readme.md", "snippet": "This file describes HTML styles.",
             "page_or_line": None, "heading": None},
        ]

        results = engine.search("h1")

        self.assertEqual([row["path"] for row in results], ["guide.md"])

    def test_short_identifier_search_does_not_match_inside_words(self) -> None:
        engine = HybridEngine({}, db=None)
        engine._rows = [
            {"file_name": "guide.md", "path": "guide.md", "snippet": "The word h10 is here.",
             "page_or_line": None, "heading": None},
        ]

        self.assertEqual(engine.search("h1"), [])

    def test_close_releases_chroma_client_and_all_heavy_references(self) -> None:
        class Client:
            closed = False

            def close(self):
                self.closed = True

        engine = HybridEngine({}, db=None)
        client = Client()
        engine._client = client
        engine._collection = object()
        engine._embedder = object()
        engine._bm25 = engine._tokens = engine._corpus = engine._rows = object()

        engine.close()

        self.assertTrue(client.closed)
        self.assertIsNone(engine._client)
        self.assertIsNone(engine._collection)
        self.assertIsNone(engine._embedder)
        self.assertIsNone(engine._bm25)
        self.assertIsNone(engine._tokens)
        self.assertIsNone(engine._corpus)
        self.assertIsNone(engine._rows)

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
