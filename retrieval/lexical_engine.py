"""Embedding-free retrieval for demo mode: SQLite FTS5 only, no FastEmbed or ChromaDB."""
from __future__ import annotations

from typing import Any

from retrieval.hybrid_engine import HybridEngine


class LexicalEngine(HybridEngine):
    """Drop-in for ``HybridEngine`` that ranks with BM25 over the FTS5 index alone."""

    def activate(self) -> None:
        return None

    def upsert(self, file_path, chunks, *, content_hash: str, refresh_lexical: bool = True) -> None:
        return None  # the database triggers keep the FTS index current

    def delete_obsolete(self, file_path, keep_ids) -> None:
        return None

    def delete(self, path, *, refresh_lexical: bool = True) -> None:
        return None

    def _search_locked(self, query: str, limit: int) -> list[dict[str, Any]]:
        text = query.strip()
        if not text:
            return []
        if len(text.split()) == 1 and len(text) <= 3:
            return self._search_short_token(text, limit)
        rows = list(self.db.search_lexical(text, max(limit * 4, 20), self._prefixes()))
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, 1):
            key = str(row["path"]).lower()
            if key in seen:
                continue
            seen.add(key)
            results.append({"path": row["path"], "snippet": row["snippet"], "score": 1.0 / (60 + rank),
                            "page_or_line": row["page_or_line"], "heading": row["heading"]})
            if len(results) >= limit:
                break
        return results

    def close(self) -> None:
        return None
