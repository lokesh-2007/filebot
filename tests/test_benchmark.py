"""Memory benchmark reporting stays separated and deterministic."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.benchmark_chat import (_MemorySampler, _is_local_ollama_url,
                                    _ollama_loaded_memory)


class BenchmarkReportingTests(unittest.TestCase):
    def test_benchmark_url_requires_explicit_loopback_address(self) -> None:
        self.assertTrue(_is_local_ollama_url("http://127.0.0.1:11435"))
        self.assertTrue(_is_local_ollama_url("http://[::1]:11435/"))
        self.assertFalse(_is_local_ollama_url("http://127.0.0.1.attacker:11435"))
        self.assertFalse(_is_local_ollama_url("http://127.0.0.1:11435@example.com"))
        self.assertFalse(_is_local_ollama_url("https://localhost:11435"))
        self.assertFalse(_is_local_ollama_url("http://localhost"))

    def test_ollama_api_model_and_vram_sizes_are_reported_separately(self) -> None:
        with patch("scripts.benchmark_chat._json_request", return_value={"models": [
            {"size": 2 * 1024 * 1024, "size_vram": 1024 * 1024},
            {"size": "bad", "size_vram": None},
        ]}):
            report = _ollama_loaded_memory("http://127.0.0.1:11434")
        self.assertEqual(report["loaded_model_count"], 2)
        self.assertEqual(report["loaded_model_size_mib"], 2.0)
        self.assertEqual(report["loaded_model_vram_mib"], 1.0)

    def test_stack_peak_uses_simultaneous_process_samples(self) -> None:
        sampler = _MemorySampler("http://127.0.0.1:11434")
        sampler._process_samples = [
            {"filebot_private_commit_mib": 100.0, "ollama_private_commit_mib": 200.0,
             "filebot_working_set_mib": 90.0, "ollama_working_set_mib": 180.0},
            {"filebot_private_commit_mib": 300.0, "ollama_private_commit_mib": 100.0,
             "filebot_working_set_mib": 270.0, "ollama_working_set_mib": 90.0},
        ]
        report = sampler.report()["process_peak_mib"]
        self.assertEqual(report["chatbot_private_commit_mib"], 400.0)
        self.assertEqual(report["chatbot_working_set_sum_mib"], 360.0)


if __name__ == "__main__":
    unittest.main()
