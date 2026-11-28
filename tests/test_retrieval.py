"""Tests for hybrid rank fusion without initializing heavyweight dependencies."""
from __future__ import annotations

import unittest
import tempfile
import sys
from types import ModuleType
from unittest.mock import patch
import zipfile
from pathlib import Path
from types import SimpleNamespace

from retrieval.hybrid_engine import HybridEngine
from retrieval.parsers import parse_file
from storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


class ReciprocalRankFusionTests(unittest.TestCase):
    def test_large_corpus_uses_bounded_sqlite_fallback_instead_of_bm25_memory(self) -> None:
        row = {"chunk_id": "older", "path": "older.txt", "file_name": "older.txt",
               "snippet": "The older project contains the target phrase.",
               "page_or_line": None, "heading": None}

        class FakeDatabase:
            def all_chunks(self, limit=None):
                self.row_limit = limit
                return [{**row, "chunk_id": f"chunk-{index}"} for index in range(limit)]

            def search_lexical(self, _query, _limit):
                return [row]

        database = FakeDatabase()
        engine = HybridEngine({"retrieval": {"max_in_memory_chunks": 2}}, database)
        with self.assertLogs("retrieval.hybrid_engine", level="WARNING"):
            engine._refresh_bm25()
        results = engine.search("older project target")

        self.assertEqual(database.row_limit, 3)
        self.assertEqual(engine._rows, [])
        self.assertIsNone(engine._bm25)
        self.assertEqual([item["path"] for item in results], ["older.txt"])

    def test_short_token_search_falls_back_to_sqlite_when_bm25_is_capped(self) -> None:
        row = {"chunk_id": "h1", "path": "guide.md", "file_name": "guide.md",
               "snippet": "Use an h1 heading here.", "page_or_line": None, "heading": None}

        class FakeDatabase:
            def search_lexical(self, query, limit):
                self.query, self.limit = query, limit
                return [row]

        database = FakeDatabase()
        engine = HybridEngine({"retrieval": {"max_in_memory_chunks": 1}}, database)
        self.assertEqual([item["path"] for item in engine.search("h1")], ["guide.md"])
        self.assertEqual(database.query, "h1")

    def test_large_corpus_keeps_dense_vector_results_when_bm25_is_capped(self) -> None:
        lexical_row = {"chunk_id": "lexical", "path": "older.txt", "file_name": "older.txt",
                       "snippet": "Exact SQLite phrase", "page_or_line": None, "heading": None}

        class FakeDatabase:
            def all_chunks(self, limit=None):
                return [{"chunk_id": f"chunk-{index}"} for index in range(limit)]

            def search_lexical(self, _query, _limit):
                return [lexical_row]

        engine = HybridEngine({"retrieval": {"max_in_memory_chunks": 1}}, FakeDatabase())
        engine._refresh_bm25()
        engine._embedder = SimpleNamespace(
            query_embed=lambda _query: [SimpleNamespace(tolist=lambda: [0.0])],
        )
        engine._collection = SimpleNamespace(query=lambda **_kwargs: {
            "ids": [["dense"]], "documents": [["Semantic match"]],
            "metadatas": [[{"path": "semantic.pdf"}]],
            "distances": [[0.2]],
        })

        results = engine.search("semantic question")
        self.assertEqual({item["path"] for item in results}, {"older.txt", "semantic.pdf"})

    def test_vector_search_drops_semantically_distant_results(self) -> None:
        class FakeDatabase:
            def search_lexical(self, _query, _limit):
                return []

        engine = HybridEngine({}, FakeDatabase())
        engine._embedder = SimpleNamespace(
            query_embed=lambda _query: [SimpleNamespace(tolist=lambda: [0.0])],
        )
        engine._collection = SimpleNamespace(query=lambda **_kwargs: {
            "ids": [["irrelevant"]], "documents": [["Unrelated benchmark boilerplate"]],
            "metadatas": [[{"path": "unrelated.txt"}]], "distances": [[0.45]],
        })

        self.assertEqual(engine.search("Reply with the single word READY."), [])

    def test_shared_high_rank_result_receives_both_votes(self) -> None:
        rows = [
            {"chunk_id": "a", "path": "a.txt", "snippet": "alpha", "page_or_line": None, "heading": None},
            {"chunk_id": "b", "path": "b.txt", "snippet": "beta", "page_or_line": "page 2", "heading": "Beta"},
            {"chunk_id": "c", "path": "c.txt", "snippet": "gamma", "page_or_line": None, "heading": None},
        ]
        results = HybridEngine.reciprocal_rank_fusion(["a", "b", "c"], ["b", "a"], rows, 3)
        self.assertEqual([item["path"] for item in results], ["a.txt", "b.txt", "c.txt"])
        self.assertGreater(results[0]["score"], results[2]["score"])

    def test_sqlite_lexical_search_survives_missing_embedding_runtime(self) -> None:
        row = {"chunk_id": "warranty", "path": "warranty.txt", "file_name": "warranty.txt",
               "snippet": "The warranty lasts two years.", "page_or_line": None,
               "heading": None}

        class FakeDatabase:
            def search_lexical(self, query, _limit):
                self.query = query
                return [row]

        database = FakeDatabase()
        engine = HybridEngine({}, database)

        results = engine.search("warranty duration")

        self.assertEqual(database.query, "warranty duration")
        self.assertEqual([item["path"] for item in results], ["warranty.txt"])
        self.assertIn("two years", results[0]["snippet"])

    def test_engine_activation_degrades_to_lexical_search_if_embeddings_fail(self) -> None:
        row = {"chunk_id": "warranty", "path": "warranty.txt", "file_name": "warranty.txt",
               "snippet": "The warranty lasts two years.", "page_or_line": None,
               "heading": None, "open_count": 0, "upvotes": 0, "downvotes": 0}

        class FakeDatabase:
            def all_chunks(self, limit=None):
                return [row]

            def search_lexical(self, _query, _limit):
                return [row]

        class BrokenTextEmbedding:
            def __init__(self, **_kwargs):
                raise RuntimeError("embedding model files are unavailable")

        fastembed = ModuleType("fastembed")
        fastembed.TextEmbedding = BrokenTextEmbedding
        settings = {"models": {
            "embeddings": {"model": "test-model", "cache_dir": "cache"},
            "vector_store": {"path": "vectors"},
        }}
        engine = HybridEngine(settings, FakeDatabase())

        with patch.dict(sys.modules, {"fastembed": fastembed, "bm25s": None}), self.assertLogs(
            "retrieval.hybrid_engine", level="ERROR"
        ):
            engine.activate()
        results = engine.search("warranty duration")

        self.assertIsNone(engine._embedder)
        self.assertEqual([item["path"] for item in results], ["warranty.txt"])

    def test_limit_and_source_metadata_are_preserved(self) -> None:
        rows = [{"chunk_id": "x", "path": "x.pdf", "snippet": "passage", "page_or_line": "page 4", "heading": "Summary"}]
        result = HybridEngine.reciprocal_rank_fusion(["x"], [], rows, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["page_or_line"], "page 4")
        self.assertEqual(result[0]["heading"], "Summary")

    def test_user_feedback_boosts_retrieval_without_overwhelming_relevance(self) -> None:
        rows = [
            {"chunk_id": "a", "path": "a.txt", "snippet": "candidate a", "page_or_line": None,
             "heading": None, "open_count": 0, "upvotes": 0, "downvotes": 0},
            {"chunk_id": "b", "path": "b.txt", "snippet": "candidate b", "page_or_line": None,
             "heading": None, "open_count": 1, "upvotes": 1, "downvotes": 0},
        ]
        result = HybridEngine.reciprocal_rank_fusion(["a", "b"], [], rows, 2)
        self.assertEqual([item["path"] for item in result], ["b.txt", "a.txt"])


class ParserResourceLimitTests(unittest.TestCase):
    def test_tree_sitter_chunks_source_by_syntax_nodes_with_line_context(self) -> None:
        class Registry:
            def has_parser(self, language):
                return language == "python"

            def process(self, source, config):
                self.source, self.config = source, config
                first = source.index("def first")
                second = source.index("def second")
                self.result = SimpleNamespace(chunks=[
                    SimpleNamespace(content=source[first:second], start_line=0, end_line=2,
                                    metadata=SimpleNamespace(symbols_defined=["first"])),
                    SimpleNamespace(content=source[second:], start_line=4, end_line=6,
                                    metadata=SimpleNamespace(symbols_defined=["second"])),
                ])
                return self.result

        class LanguageRegistry:
            registry = Registry()

            @classmethod
            def default(cls):
                return cls.registry

        module = ModuleType("tree_sitter_language_pack")
        module.LanguageRegistry = LanguageRegistry
        module.ProcessConfig = lambda **kwargs: SimpleNamespace(**kwargs)
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "example.py"
            source = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
            path.write_text(source, encoding="utf-8")
            with patch.dict(sys.modules, {"tree_sitter_language_pack": module}):
                chunks = parse_file(path, chunk_chars=100, overlap=10, source_parse_timeout_ms=75)

        self.assertEqual(LanguageRegistry.registry.config.parse_timeout_ms, 75)
        self.assertEqual(LanguageRegistry.registry.config.chunk_max_size, 100)
        self.assertEqual(len(chunks), 2)
        self.assertIn("def first", chunks[0].text)
        self.assertIn("def second", chunks[1].text)
        self.assertEqual(chunks[0].page_or_line, "lines 1-3")
        self.assertEqual(chunks[1].heading, "second")

    def test_missing_cached_tree_sitter_grammar_falls_back_without_download(self) -> None:
        class Registry:
            def has_parser(self, _language):
                return False

            def process(self, *_args, **_kwargs):
                self.fail("uncached grammars must not trigger downloads")

        class LanguageRegistry:
            @staticmethod
            def default():
                return Registry()

        module = ModuleType("tree_sitter_language_pack")
        module.LanguageRegistry = LanguageRegistry
        module.ProcessConfig = lambda **kwargs: SimpleNamespace(**kwargs)
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "example.rs"
            path.write_text("fn main() { println!(\"ok\"); }", encoding="utf-8")
            with patch.dict(sys.modules, {"tree_sitter_language_pack": module}):
                chunks = parse_file(path, chunk_chars=100, overlap=0)
        self.assertIn("fn main", chunks[0].text)
        self.assertEqual(chunks[0].page_or_line, "source")

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
    def test_missing_embedding_batch_uses_eight_chunk_batches(self) -> None:
        class FakeEmbedder:
            def __init__(self):
                self.batch_sizes = []

            def embed(self, texts):
                self.batch_sizes.append(len(texts))
                return [SimpleNamespace(tolist=lambda: [0.0]) for _ in texts]

        class FakeCollection:
            def upsert(self, **_kwargs):
                return None

        engine = HybridEngine({"models": {"embeddings": {}}}, object())
        embedder = FakeEmbedder()
        engine._embedder = embedder
        engine._collection = FakeCollection()

        engine.upsert(Path("notes.txt"), [SimpleNamespace(text=f"line {i}") for i in range(9)],
                      content_hash="test", refresh_lexical=False)

        self.assertEqual(embedder.batch_sizes, [8, 1])

    def test_bm25_tokenization_disables_console_progress_for_pythonw(self) -> None:
        calls = []

        class FakeBM25:
            def index(self, tokens, **kwargs):
                self.tokens = tokens
                self.index_kwargs = kwargs

            def retrieve(self, _tokens, **kwargs):
                self.retrieve_kwargs = kwargs
                return [[0]], [[1.0]]

        module = ModuleType("bm25s")
        module.BM25 = FakeBM25

        def tokenize(texts, **kwargs):
            calls.append((texts, kwargs))
            return ["tokenized"]

        module.tokenize = tokenize

        class FakeDatabase:
            def all_chunks(self, limit=None):
                return [{"chunk_id": "chunk", "path": "doc.txt", "file_name": "doc.txt",
                         "snippet": "matching terms", "page_or_line": None, "heading": None}]

            def search_lexical(self, *_args):
                return []

        engine = HybridEngine({}, FakeDatabase())
        with patch.dict(sys.modules, {"bm25s": module}):
            engine._refresh_bm25()
            engine._embedder = SimpleNamespace(
                query_embed=lambda _query: [SimpleNamespace(tolist=lambda: [0.0])],
            )
            engine._collection = SimpleNamespace(query=lambda **_kwargs: {"ids": [[]]})
            result = engine.search("matching terms")

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(not kwargs["show_progress"] for _, kwargs in calls))
        self.assertFalse(engine._bm25.index_kwargs["show_progress"])
        self.assertFalse(engine._bm25.retrieve_kwargs["show_progress"])
        self.assertIsNotNone(engine._bm25)
        self.assertEqual(result[0]["path"], "doc.txt")

    def test_lexical_fallback_works_without_stdout_when_bm25_is_unavailable(self) -> None:
        class FakeDatabase:
            def all_chunks(self, limit=None):
                return [{"chunk_id": "chunk", "path": "doc.txt", "file_name": "doc.txt",
                         "snippet": "matching terms", "page_or_line": None, "heading": None}]

            def search_lexical(self, *_args):
                return [{"chunk_id": "chunk", "path": "doc.txt", "file_name": "doc.txt",
                         "snippet": "matching terms", "page_or_line": None, "heading": None}]

        engine = HybridEngine({}, FakeDatabase())
        engine._embedder = SimpleNamespace(
            query_embed=lambda _query: [SimpleNamespace(tolist=lambda: [0.0])],
        )
        engine._collection = SimpleNamespace(query=lambda **_kwargs: {"ids": [[]]})

        with patch.dict(sys.modules, {"bm25s": None}), patch("sys.stdout", None):
            engine._refresh_bm25()
            result = engine._search_locked("matching terms", 1)

        self.assertIsNone(engine._bm25)
        self.assertEqual(result[0]["path"], "doc.txt")

    def test_edited_glossary_and_feedback_on_representative_corpus(self) -> None:
        """Exercise retrieval controls end to end without downloading models."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            root = Path(directory)
            db = Database(root / "filebot.sqlite3")
            db.initialize()
            documents = [
                ("api-reference.md", "Product specification for the REST API and auth headers."),
                ("retention-policy.md", "Records retention policy: keep audit logs. This retention policy governs records for 30 days."),
                ("retention-policy-draft.md", "Draft retention policy for archived records."),
                ("incident-runbook.md", "Incident recovery steps for a failed nightly backup."),
                ("budget-notes.md", "Quarterly budget forecast and vendor costs."),
            ]
            for name, content in documents:
                db.replace_file(root / name, content_hash=name, size=len(content), mtime=1.0,
                                chunks=[{"text": content}])

            # Model outputs are intentionally empty: this verifies the shipped
            # SQLite lexical fallback and keeps the check deterministic/offline.
            engine = HybridEngine({}, db)
            engine._refresh_bm25()
            engine._embedder = SimpleNamespace(
                query_embed=lambda _query: [SimpleNamespace(tolist=lambda: [0.0])],
            )
            engine._collection = SimpleNamespace(query=lambda **_kwargs: {"ids": [[]]})

            initial = engine.search("pbrief")
            self.assertEqual(initial, [])
            db.replace_glossary_entries({"pbrief": "product specification"})
            edited = engine.search("pbrief")
            self.assertEqual(Path(edited[0]["path"]).name, "api-reference.md")
            self.assertEqual(len(edited), 1)

            before = engine.search("retention policy")
            self.assertEqual(Path(before[0]["path"]).name, "retention-policy.md")
            db.record_file_feedback(root / "retention-policy-draft.md", "up")
            engine.refresh_file_signal(root / "retention-policy-draft.md")
            after = engine.search("retention policy")
            self.assertEqual(Path(after[0]["path"]).name, "retention-policy-draft.md")

            short_id_corpus = [
                {"chunk_id": "id", "file_name": "config.md", "path": "config.md",
                 "snippet": "Set API timeout to 30 seconds.", "page_or_line": None,
                 "heading": None, "open_count": 0, "upvotes": 0, "downvotes": 0},
                {"chunk_id": "word", "file_name": "notes.md", "path": "notes.md",
                 "snippet": "This word appears inside rapidity.", "page_or_line": None,
                 "heading": None, "open_count": 0, "upvotes": 0, "downvotes": 0},
            ]
            exact = HybridEngine.reciprocal_rank_fusion(["id", "word"], [], short_id_corpus, 2)
            self.assertEqual(exact[0]["path"], "config.md")

    def test_engine_expands_database_glossary_before_retrieval(self) -> None:
        class FakeDatabase:
            def expand_glossary(self, query):
                return query.replace("spec", "product specification")

        engine = HybridEngine({}, FakeDatabase())
        from unittest.mock import patch
        with patch.object(engine, "_search_locked", return_value=[]) as search:
            self.assertEqual(engine.search("spec", limit=4), [])
        search.assert_called_once_with("product specification", 4)

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
                def all_chunks(self, limit=None):
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
