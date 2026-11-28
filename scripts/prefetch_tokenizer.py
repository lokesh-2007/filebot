"""Cache Nanobot's local prompt tokenizer for reliable offline context checks."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from filebot_config import expand_config_path, load_settings


def main() -> int:
    settings = load_settings(ROOT / "config" / "settings.yaml")
    database_path = expand_config_path(settings["app"]["database_path"])
    cache = database_path.parent / "nanobot" / "cache" / "tiktoken"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    try:
        import tiktoken

        tiktoken.get_encoding("cl100k_base")
    except Exception as exc:
        raise SystemExit(
            "Could not cache Nanobot's prompt tokenizer. Check the network and rerun setup. "
            f"Details: {exc}"
        ) from exc
    print(f"Cached the local prompt tokenizer at {cache}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
