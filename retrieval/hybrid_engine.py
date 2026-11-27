"""Lazy local vector + SQLite FTS5 retrieval fused with reciprocal rank fusion."""
from __future__ import annotations

import logging
import hashlib
import re
import threading
from pathlib import Path
from typing import Any

from filebot_config import IndexScope, expand_config_path
from resources import resolve_resource
from storage.db import Database

log = logging.getLogger(__name__)


class HybridEngine:
    """Owns heavy embedding/search objects only while the assistant is awake."""
    def __init__(self, settings: dict[str, Any], db: Database) -> None:
        self.settings, self.db = settings, db
        self.scope = IndexScope.from_settings(settings)
        self._restrict_roots = "roots" in settings.get("index", {})
        self._embedder = self._collection = self._client = None
        self._lock = threading.RLock()

    def activate(self) -> None:
        with self._lock:
            from fastembed import TextEmbedding
            import chromadb
            from chromadb.config import Settings as ChromaSettings
            model = self.settings["models"]["embeddings"]
            kwargs: dict[str, Any] = {}
            bundled = model.get("model_dir")
            if bundled:
                model_dir = resolve_resource(bundled)
                if model_dir.is_dir() and any(model_dir.glob("*.onnx")):
                    # Fully offline: load the shipped ONNX folder, never touch the network.
                    kwargs = {"specific_model_path": str(model_dir), "local_files_only": True}
                else:
                    log.warning("Bundled embedding model folder %s is missing or empty; falling back to the "
                                "download cache (needs network on first use)", model_dir)
            self._embedder = TextEmbedding(model_name=model["model"],
                                           cache_dir=str(expand_config_path(model["cache_dir"])), **kwargs)
            vector_path = expand_config_path(self.settings["models"]["vector_store"]["path"])
            vector_path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=str(vector_path), settings=ChromaSettings(anonymized_telemetry=False))
            self._collection = self._open_collection(str(model["model"]))

    def _open_collection(self, model_name: str) -> Any:
        """Open the vector collection, rebuilding it when the embedding model changed.

        Vectors from different models (or dimensions) are not comparable, so the
        collection is stamped with the model name and dimension. A missing or
        different stamp drops the vectors and requeues every indexed file.
        """
        dimension = len(next(iter(self._embedder.embed(["dimension probe"]))))
        stamp = {"hnsw:space": "cosine", "embedding_model": model_name, "embedding_dim": dimension}
        name = "filebot_chunks"
        try:
            existing = self._client.get_collection(name=name)
        except Exception:
            existing = None
        if existing is not None:
            meta = existing.metadata or {}
            if meta.get("embedding_model") == model_name and meta.get("embedding_dim") == dimension:
                return existing
            log.warning("Embedding model changed (%s, dim %s); rebuilding the vector index",
                        model_name, dimension)
            self._client.delete_collection(name=name)
        collection = self._client.create_collection(name=name, metadata=stamp)
        queued = self.db.requeue_all_files()
        if queued:
            log.info("Requeued %d indexed files for re-embedding", queued)
        return collection

    def upsert(self, file_path: Path, chunks: list[Any], *, content_hash: str,
               refresh_lexical: bool = True) -> None:  # refresh_lexical: accepted for compatibility
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

    def delete_obsolete(self, file_path: str | Path, keep_ids: list[str]) -> None:
        """Delete stale vectors only after their replacement is committed in SQLite."""
        with self._lock:
            normalized = str(Path(file_path).expanduser().resolve())
            existing = self._collection.get(where={"path": normalized}, include=[]).get("ids", [])
            keep = set(keep_ids)
            obsolete = [chunk_id for chunk_id in existing if chunk_id not in keep]
            if obsolete:
                self._collection.delete(ids=obsolete)

    def delete(self, path: str | Path, *, refresh_lexical: bool = True) -> None:  # flag unused (FTS5)
        with self._lock:
            file = str(Path(path).resolve())
            found = self._collection.get(where={"path": file}, include=[])
            if found.get("ids"):
                self._collection.delete(ids=found["ids"])

    def refresh_lexical_index(self) -> None:
        """No-op kept for callers: the FTS5 index is updated transactionally by the database."""

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
        for chunk_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]:
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
            return self._search_locked(query, limit)

    def _prefixes(self) -> list[str] | None:
        """Directory prefixes limiting results to current roots (None = unrestricted)."""
        return self.scope.prefixes() if self._restrict_roots else None

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
        prefixes = self._prefixes()
        dense: list[str] = []
        try:
            vector = list(self._embedder.query_embed(query))[0].tolist()
            result = self._collection.query(query_embeddings=[vector], n_results=take, include=["documents", "metadatas"])
            dense = [chunk_id for chunk_id in (result.get("ids") or [[]])[0]]
        except Exception:
            log.exception("Vector search failed")
        lexical_rows = list(self.db.search_lexical(query, take, prefixes))
        lexical = [str(row["chunk_id"]) for row in lexical_rows]
        # Vector hits are resolved through SQLite so stale vectors and paths outside
        # the current roots drop out; rows already fetched lexically are reused.
        lookup = {str(row["chunk_id"]): row for row in lexical_rows}
        missing = [chunk_id for chunk_id in dense if chunk_id not in lookup]
        if missing:
            lookup.update({str(row["chunk_id"]): row for row in self.db.chunks_by_ids(missing, prefixes)})
        dense = [chunk_id for chunk_id in dense if chunk_id in lookup]
        fused = self.reciprocal_rank_fusion(dense, lexical, list(lookup.values()), len(lookup))
        # One result per file: keep each file's best-scoring chunk.
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for item in fused:
            key = str(item["path"]).lower()
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def _search_short_token(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Rank exact short-token hits among candidate rows from the SQLite index."""
        pattern = re.compile(rf"(?<!\w){re.escape(query)}(?!\w)", re.IGNORECASE)
        matches: list[tuple[int, int, Any]] = []
        for position, row in enumerate(self.db.search_exact_token(query, 2000, self._prefixes())):
            text = f"{row['file_name']}\n{row['snippet']}"
            count = sum(1 for _ in pattern.finditer(text))
            if count:
                matches.append((count, position, row))
        matches.sort(key=lambda item: (-item[0], item[1]))
        return [{
            "path": row["path"],
            "snippet": row["snippet"],
            "score": count / (count + 1.0),
            "page_or_line": row["page_or_line"],
            "heading": row["heading"],
        } for count, _, row in matches[:max(0, limit)]]

    def close(self) -> None:
        """Release references to all heavyweight objects so they can unload."""
        with self._lock:
            client = self._client
            # Chroma's persistent client owns SQLite/native resources beyond the
            # lifetime of Python references. Close it explicitly before dropping
            # the last local handle so sleep releases file locks and memory.
            self._collection = self._client = self._embedder = None
            close_client = getattr(client, "close", None)
            if callable(close_client):
                try:
                    close_client()
                except Exception:
                    log.exception("Could not close the persistent Chroma client cleanly")
            elif client is not None:
                log.warning("Chroma client has no close() method; resources may remain until process exit")
