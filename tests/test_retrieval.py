"""Tests for hybrid rank fusion without initializing heavyweight dependencies."""
from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
