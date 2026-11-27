"""Lazy local vector + BM25 retrieval fused with reciprocal rank fusion."""
from __future__ import annotations

import logging
import hashlib
import math
import re
import threading
from pathlib import Path
from typing import Any

from filebot_config import expand_config_path, path_is_within_roots
from storage.db import Database

log = logging.getLogger(__name__)


class HybridEngine:
    """Owns heavy embedding/search objects only while the assistant is awake."""
    def __init__(self, settings: dict[str, Any], db: Database) -> None:
        self.settings, self.db = settings, db
        glossary = settings.get("retrieval", {}).get("glossary", {})
        if glossary:
            self.db.seed_glossary_entries(glossary)
        self.roots = [expand_config_path(root) for root in settings.get("index", {}).get("roots", [])]
        self._restrict_roots = "roots" in settings.get("index", {})
        self._embedder = self._collection = self._client = None
        self._bm25 = self._tokens = self._corpus = self._rows = None
        self._lock = threading.RLock()

    def activate(self) -> None:
        with self._lock:
            from fastembed import TextEmbedding
            import chromadb
            model = self.settings["models"]["embeddings"]
            self._embedder = TextEmbedding(model_name=model["model"], cache_dir=str(expand_config_path(model["cache_dir"])))
            vector_path = expand_config_path(self.settings["models"]["vector_store"]["path"])
            vector_path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(vector_path))
            self._collection = self._client.get_or_create_collection(name="filebot_chunks", metadata={"hnsw:space": "cosine"})
            self._refresh_bm25()

    def _refresh_bm25(self) -> None:
        with self._lock:
            rows = self.db.all_chunks()
            if self._restrict_roots:
                rows = [row for row in rows if path_is_within_roots(row["path"], self.roots)]
            self._rows = rows
            self._corpus = [str(row["snippet"]) for row in rows]
            if not rows:
                self._bm25 = self._tokens = None
                return
            try:
                import bm25s
                self._bm25 = bm25s.BM25()
                self._tokens = bm25s.tokenize(self._corpus, stopwords="en")
                self._bm25.index(self._tokens)
            except Exception:
                log.exception("BM25 index unavailable; lexical SQLite fallback will be used")
                self._bm25 = self._tokens = None

    def upsert(self, file_path: Path, chunks: list[Any], *, content_hash: str,
               refresh_lexical: bool = True) -> None:
        with self._lock:
            batch_size = max(1, int(self.settings["models"]["embeddings"].get("batch_size", 64)))
            normalized = str(file_path.expanduser().resolve())
            for start in range(0, len(chunks), batch_size):
                batch = chunks[start:start + batch_size]
                texts = [chunk.text for chunk in batch]
                embeddings = list(self._embedder.embed(texts))
                ids = [hashlib.sha256(
                    f"{normalized}:{content_hash}:{index}".encode("utf-8")
                ).hexdigest() for index in range(start, start + len(batch))]
                self._collection.upsert(ids=ids, embeddings=[vector.tolist() for vector in embeddings],
                    documents=texts, metadatas=[{"path": normalized, "chunk_index": i}
                                                for i in range(start, start + len(batch))])
            if refresh_lexical:
                self._refresh_bm25()

    def delete_obsolete(self, file_path: str | Path, keep_ids: list[str]) -> None:
        """Delete stale vectors only after their replacement is committed in SQLite."""
        with self._lock:
            normalized = str(Path(file_path).expanduser().resolve())
            existing = self._collection.get(where={"path": normalized}, include=[]).get("ids", [])
            keep = set(keep_ids)
            obsolete = [chunk_id for chunk_id in existing if chunk_id not in keep]
            if obsolete:
                self._collection.delete(ids=obsolete)

    def delete(self, path: str | Path, *, refresh_lexical: bool = True) -> None:
        with self._lock:
            file = str(Path(path).resolve())
            found = self._collection.get(where={"path": file}, include=[])
            if found.get("ids"):
                self._collection.delete(ids=found["ids"])
            if refresh_lexical:
                self._refresh_bm25()

    def refresh_lexical_index(self) -> None:
        """Rebuild in-memory BM25 once after a group of queued file changes."""
        with self._lock:
            self._refresh_bm25()

    def refresh_file_signal(self, path: str | Path) -> None:
        """Refresh one file's ranking counters without rebuilding the corpus index."""
        with self._lock:
            normalized = str(Path(path).expanduser().resolve())
            opens, upvotes, downvotes = self.db.file_signal_counts(normalized)
            for index, row in enumerate(self._rows or []):
                if str(Path(str(row["path"])).expanduser().resolve()) == normalized:
                    updated = dict(row)
                    updated.update(open_count=opens, upvotes=upvotes, downvotes=downvotes)
                    self._rows[index] = updated

    @staticmethod
    def reciprocal_rank_fusion(
        dense_ids: list[str], lexical_ids: list[str], rows: list[Any], limit: int, *, k: int = 60,
    ) -> list[dict[str, Any]]:
        """Fuse ranked chunk IDs and attach their stored source metadata."""
        scores: dict[str, float] = {}
        lookup = {str(row["chunk_id"]): row for row in rows}
        for ranking in (dense_ids, lexical_ids):
            for rank, chunk_id in enumerate(ranking, 1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        results: list[dict[str, Any]] = []
        adjusted: list[tuple[str, float]] = []
        for chunk_id, score in scores.items():
            row = lookup.get(chunk_id)
            if row is not None and "open_count" in row.keys():
                opens = max(0, int(row["open_count"]))
                upvotes = max(0, int(row["upvotes"]))
                downvotes = max(0, int(row["downvotes"]))
                score += max(-0.01, min(0.008, 0.001 * math.log1p(opens)
                                         + 0.003 * (upvotes - downvotes)))
            adjusted.append((chunk_id, score))
        for chunk_id, score in sorted(adjusted, key=lambda item: item[1], reverse=True)[:limit]:
            row = lookup.get(chunk_id)
            results.append({
                "path": row["path"] if row else "",
                "snippet": row["snippet"] if row else "",
                "score": score,
                "page_or_line": row["page_or_line"] if row else None,
                "heading": row["heading"] if row else None,
            })
        return results

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            expand = getattr(self.db, "expand_glossary", None)
            expanded_query = expand(query) if callable(expand) else query
            return self._search_locked(expanded_query, limit)

    def _search_locked(self, query: str, limit: int) -> list[dict[str, Any]]:
        normalized_query = query.strip()
        if not normalized_query:
            return []
        # Dense embeddings blur terse identifiers such as h1, API, and C#.
        # For a single short token, return only exact token matches so unrelated
        # documents do not outrank an empty result through semantic similarity.
        if len(normalized_query.split()) == 1 and len(normalized_query) <= 3:
            return self._search_short_token(normalized_query, limit)
        if self._embedder is None:
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
        rows = list(self._rows or [])
        known_ids = {str(row["chunk_id"]) for row in rows}
        if self._bm25 is not None and self._tokens is not None and self._rows:
            try:
                tokens = __import__("bm25s").tokenize([query], stopwords="en")
                _, indexes = self._bm25.retrieve(tokens, k=min(take, len(self._rows)))
                for index in indexes[0]:
                    if int(index) >= 0:
                        row = self._rows[int(index)]
                        key = str(row["chunk_id"])
                        lexical.append(key)
            except Exception:
                log.exception("BM25 search failed")
        if not lexical:
            for row in self.db.search_lexical(query, take):
                if self._restrict_roots and not path_is_within_roots(row["path"], self.roots):
                    continue
                key = str(row["chunk_id"])
                lexical.append(key)
                if key not in known_ids:
                    rows.append(row)
                    known_ids.add(key)
        return self.reciprocal_rank_fusion(dense, lexical, rows, limit)

    def _search_short_token(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Rank exact short-token hits in the already-loaded SQLite chunk rows."""
        pattern = re.compile(rf"(?<!\w){re.escape(query)}(?!\w)", re.IGNORECASE)
        matches: list[tuple[int, float, int, Any]] = []
        for position, row in enumerate(self._rows or []):
            text = f"{row['file_name']}\n{row['snippet']}"
            count = sum(1 for _ in pattern.finditer(text))
            if count:
                boost = 0.0
                if "open_count" in row.keys():
                    boost = max(-0.01, min(
                        0.008,
                        0.001 * math.log1p(max(0, int(row["open_count"])))
                        + 0.003 * (int(row["upvotes"]) - int(row["downvotes"])),
                    ))
                matches.append((count, boost, position, row))
        matches.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [{
            "path": row["path"],
            "snippet": row["snippet"],
            "score": count / (count + 1.0) + boost,
            "page_or_line": row["page_or_line"],
            "heading": row["heading"],
        } for count, boost, _, row in matches[:max(0, limit)]]

    def close(self) -> None:
        """Release references to all heavyweight objects so they can unload."""
        with self._lock:
            client = self._client
            # Chroma's persistent client owns SQLite/native resources beyond the
            # lifetime of Python references. Close it explicitly before dropping
            # the last local handle so sleep releases file locks and memory.
            self._collection = self._client = self._embedder = None
            self._bm25 = self._tokens = self._corpus = self._rows = None
            close_client = getattr(client, "close", None)
            if callable(close_client):
                try:
                    close_client()
                except Exception:
                    log.exception("Could not close the persistent Chroma client cleanly")
            elif client is not None:
                log.warning("Chroma client has no close() method; resources may remain until process exit")
