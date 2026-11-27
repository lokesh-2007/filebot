"""Tests for hybrid rank fusion without initializing heavyweight dependencies."""
from __future__ import annotations

import unittest
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

from retrieval.hybrid_engine import HybridEngine
from retrieval.parsers import parse_file
from storage.db import Database

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
    def _db_with(self, directory: Path, files: dict[str, str]) -> "Database":
        db = Database(directory / "fb.sqlite3")
        db.initialize()
        for index, (path, text) in enumerate(files.items()):
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            db.replace_file(target, content_hash=f"h{index}", size=len(text), mtime=1.0,
                            chunks=[{"text": text, "page_or_line": None, "heading": None}])
        return db

    def _engine(self, db, roots, *, dense_ids=()):
        class Vector:
            def tolist(self):
                return [0.1]

        class Embedder:
            def query_embed(self, query):
                return [Vector()]

        class Collection:
            def query(self, **kwargs):
                return {"ids": [list(dense_ids)]}

        engine = HybridEngine({"index": {"roots": [str(root) for root in roots]}}, db)
        engine._embedder, engine._collection = Embedder(), Collection()
        return engine

    def test_search_hides_stale_rows_outside_current_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            db = self._db_with(base, {str(base / "allowed" / "in.txt"): "matching content here",
                                      str(base / "outside" / "old.txt"): "matching content here"})
            outside_id = db.vector_ids_for_file(base / "outside" / "old.txt")
            engine = self._engine(db, [base / "allowed"], dense_ids=outside_id)
            results = engine.search("matching content")
            self.assertEqual([Path(item["path"]).name for item in results], ["in.txt"])

    def test_dense_only_hits_are_resolved_and_stale_vectors_dropped(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            db = self._db_with(base, {str(base / "a.txt"): "semantic neighbour"})
            real = db.vector_ids_for_file(base / "a.txt")
            engine = self._engine(db, [base], dense_ids=[*real, "stale-vector-id"])
            results = engine.search("totally different words")
            self.assertEqual([Path(item["path"]).name for item in results], ["a.txt"])

    def test_results_are_deduplicated_per_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            db = Database(base / "fb.sqlite3")
            db.initialize()
            target = base / "multi.txt"
            target.write_text("x", encoding="utf-8")
            db.replace_file(target, content_hash="h", size=1, mtime=1.0, chunks=[
                {"text": "budget alpha", "page_or_line": None, "heading": None},
                {"text": "budget beta", "page_or_line": None, "heading": None}])
            results = self._engine(db, [base]).search("budget")
            self.assertEqual(len(results), 1)

    def test_short_identifier_search_returns_exact_hits_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            db = self._db_with(base, {str(base / "guide.md"): "Use an h1 heading here.",
                                      str(base / "readme.md"): "This file describes HTML styles."})
            results = self._engine(db, [base]).search("h1")
            self.assertEqual([Path(row["path"]).name for row in results], ["guide.md"])

    def test_short_identifier_search_does_not_match_inside_words(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            db = self._db_with(base, {str(base / "guide.md"): "The word h10 is here."})
            self.assertEqual(self._engine(db, [base]).search("h1"), [])

    def test_short_identifier_with_punctuation_is_found(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            db = self._db_with(base, {str(base / "lang.md"): "We write C# daily.",
                                      str(base / "other.md"): "The C compiler."})
            results = self._engine(db, [base]).search("C#")
            self.assertEqual([Path(row["path"]).name for row in results], ["lang.md"])

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

        engine.close()

        self.assertTrue(client.closed)
        self.assertIsNone(engine._client)
        self.assertIsNone(engine._collection)
        self.assertIsNone(engine._embedder)

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
