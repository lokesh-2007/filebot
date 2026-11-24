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
        return self.engine.search(query, max(1, min(int(limit), 50)))
