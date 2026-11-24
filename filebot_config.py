"""Validated FileBot YAML settings and Windows-friendly path expansion."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def load_settings(path: str | Path) -> dict[str, Any]:
    """Load settings and reject malformed top-level sections with a useful error."""
    import yaml

    source = Path(path)
    try:
        settings = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Cannot load FileBot settings from {source}: {exc}") from exc
    if not isinstance(settings, dict):
        raise RuntimeError(f"FileBot settings must contain a YAML mapping: {source}")
    for section in ("app", "index", "models", "runtime"):
        if not isinstance(settings.get(section), dict):
            raise RuntimeError(f"FileBot settings section '{section}' must be a mapping")
    for section, required in {
        "app": ("database_path",),
        "index": ("roots", "include_extensions"),
        "runtime": ("hotkey",),
    }.items():
        missing = [key for key in required if key not in settings[section]]
        if missing:
            raise RuntimeError(f"FileBot settings section '{section}' is missing: {', '.join(missing)}")
    models = settings["models"]
    for section in ("ollama", "embeddings", "vector_store"):
        if not isinstance(models.get(section), dict):
            raise RuntimeError(f"FileBot settings section 'models.{section}' must be a mapping")
    for section, required in {
        "ollama": ("base_url", "model"),
        "embeddings": ("model", "cache_dir"),
        "vector_store": ("path",),
    }.items():
        missing = [key for key in required if key not in models[section]]
        if missing:
            raise RuntimeError(f"FileBot settings section 'models.{section}' is missing: {', '.join(missing)}")
    return settings


def expand_config_path(value: str | Path) -> Path:
    """Expand ``~`` and environment variables in a configured path."""
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    if not expanded.strip():
        raise ValueError("Configured path cannot be empty")
    return Path(expanded)

