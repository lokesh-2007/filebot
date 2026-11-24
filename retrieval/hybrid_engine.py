"""Lazy local vector + BM25 retrieval fused with reciprocal rank fusion."""
from __future__ import annotations

import logging
import hashlib
from pathlib import Path
from typing import Any

from storage.db import Database

log = logging.getLogger(__name__)


class HybridEngine:
    """Owns heavy embedding/search objects only while the assistant is awake."""
    def __init__(self, settings: dict[str, Any], db: Database) -> None:
        self.settings, self.db = settings, db
        self._embedder = self._collection = self._client = None
        self._bm25 = self._tokens = self._corpus = self._rows = None

    def activate(self) -> None:
        from fastembed import TextEmbedding
        import chromadb
        model = self.settings["models"]["embeddings"]
        self._embedder = TextEmbedding(model_name=model["model"], cache_dir=str(Path(model["cache_dir"]).expanduser()))
        vector_path = Path(self.settings["models"]["vector_store"]["path"]).expanduser()
        vector_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(vector_path))
        self._collection = self._client.get_or_create_collection(name="filebot_chunks", metadata={"hnsw:space": "cosine"})
        self._refresh_bm25()

    def _refresh_bm25(self) -> None:
        rows = self.db.all_chunks()
        self._rows = rows
        self._corpus = [str(row["snippet"]) for row in rows]
        if not rows:
            self._bm25 = self._tokens = None
            return
        try:
            import bm25s
            self._bm25 = bm25s.BM25()
            self._tokens = bm25s.tokenize(self._corpus, stopwords="en", stemmer="english")
            self._bm25.index(self._tokens)
        except Exception:
            log.exception("BM25 index unavailable; lexical SQLite fallback will be used")
            self._bm25 = self._tokens = None

    def upsert(self, file_path: Path, chunks: list[Any], *, refresh_lexical: bool = True) -> None:
        texts = [c.text for c in chunks]
        embeddings = list(self._embedder.embed(texts)) if texts else []
        previous = self._collection.get(where={"path": str(file_path.resolve())}, include=[])
        if previous.get("ids"):
            self._collection.delete(ids=previous["ids"])
        ids = [hashlib.sha256(f"{file_path.resolve()}:{i}".encode("utf-8")).hexdigest() for i in range(len(texts))]
        if ids:
            self._collection.upsert(ids=ids, embeddings=[v.tolist() for v in embeddings], documents=texts,
                metadatas=[{"path": str(file_path.resolve()), "chunk_index": i} for i in range(len(texts))])
        if refresh_lexical:
            self._refresh_bm25()

    def delete(self, path: str | Path, *, refresh_lexical: bool = True) -> None:
        file = str(Path(path).resolve())
        try:
            found = self._collection.get(where={"path": file}, include=[])
            if found.get("ids"):
                self._collection.delete(ids=found["ids"])
        except Exception:
            log.debug("No vector records to delete for %s", file, exc_info=True)
        if refresh_lexical:
            self._refresh_bm25()

    def refresh_lexical_index(self) -> None:
        """Rebuild in-memory BM25 once after a group of queued file changes."""
        self._refresh_bm25()

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not query.strip() or self._embedder is None:
            return []
        take = max(limit * 4, 20)
        dense: list[str] = []
        try:
            vector = list(self._embedder.query_embed(query))[0].tolist()
            result = self._collection.query(query_embeddings=[vector], n_results=take, include=["documents", "metadatas"])
            dense = [chunk_id for chunk_id in (result.get("ids") or [[]])[0]]
        except Exception:
            log.exception("Vector search failed")
        lexical: list[str] = []
        lookup: dict[str, Any] = {}
        if self._bm25 is not None and self._tokens is not None and self._rows:
            try:
                tokens = __import__("bm25s").tokenize([query], stopwords="en", stemmer="english")
                _, indexes = self._bm25.retrieve(tokens, k=min(take, len(self._rows)))
                for index in indexes[0]:
                    if int(index) >= 0:
                        row = self._rows[int(index)]
                        key = str(row["chunk_id"])
                        lexical.append(key)
                        lookup[key] = row
            except Exception:
                log.exception("BM25 search failed")
        if not lexical:
            for row in self.db.search_lexical(query, take):
                key = str(row["chunk_id"])
                lexical.append(key)
                lookup[key] = row
        scores: dict[str, float] = {}
        for ranked in (dense, lexical):
            for rank, key in enumerate(ranked, 1):
                scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
        # Hydrate using a lexical lookup when available; otherwise return a path and vector snippet.
        out = []
        if self._rows:
            lookup.update({str(row["chunk_id"]): row for row in self._rows})
        for key, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]:
            row = lookup.get(key)
            out.append({"path": row["path"] if row else "",
                        "snippet": row["snippet"] if row else "", "score": score,
                        "page_or_line": row["page_or_line"] if row else None,
                        "heading": row["heading"] if row else None})
        return out

    def close(self) -> None:
        """Release references to all heavyweight objects so they can unload."""
        self._embedder = self._collection = self._client = None
        self._bm25 = self._tokens = self._corpus = self._rows = None
