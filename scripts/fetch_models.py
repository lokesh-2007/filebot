"""Download FileBot's bundled embedding model into ./models for offline use.

Run once from a source checkout (needs network):  python scripts/fetch_models.py
Packaged builds ship this folder, so end users never run it.
The chat GGUF and llama-server.exe are separate downloads; see README "Local model".
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"  # the files fastembed uses for BAAI/bge-small-en-v1.5
FILES = ["model_optimized.onnx", "config.json", "tokenizer.json",
         "tokenizer_config.json", "special_tokens_map.json"]


def main() -> int:
    from huggingface_hub import snapshot_download

    target = Path(__file__).resolve().parents[1] / "models" / "bge-small-en-v1.5-onnx"
    snapshot_download(repo_id=REPO, local_dir=str(target), allow_patterns=FILES)
    missing = [name for name in FILES if not (target / name).is_file()]
    if missing:
        print(f"Download incomplete, missing: {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"Embedding model ready in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
