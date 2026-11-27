"""Search tool used by FileBot's router and agent integrations."""
from __future__ import annotations

from typing import Any


class SearchFilesTool:
    """Search indexed local documents using hybrid retrieval."""
    name = "search_files"
    description = "Search indexed local files and return paths and relevant snippets."

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def execute(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 10))
        matches = self.engine.search(query, bounded_limit)
        bounded: list[dict[str, Any]] = []
        for match in matches[:bounded_limit]:
            if not isinstance(match, dict):
                continue
            item = dict(match)
            if "snippet" in item:
                item["snippet"] = str(item["snippet"])[:1200]
            bounded.append(item)
        return bounded
