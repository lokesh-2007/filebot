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
    index = settings["index"]
    if not isinstance(index["roots"], list) or any(not isinstance(root, str) or not root.strip()
                                                  for root in index["roots"]):
        raise RuntimeError("FileBot setting 'index.roots' must be a list of non-empty paths")
    extensions = index["include_extensions"]
    if not isinstance(extensions, list) or not extensions or any(
        not isinstance(extension, str) or not extension.startswith(".") for extension in extensions
    ):
        raise RuntimeError("FileBot setting 'index.include_extensions' must be a non-empty list of dotted extensions")
    excluded = index.get("exclude_dirs", [])
    if not isinstance(excluded, list) or any(not isinstance(name, str) or not name for name in excluded):
        raise RuntimeError("FileBot setting 'index.exclude_dirs' must be a list of directory names")
    _validate_number(index.get("max_file_bytes", 50 * 1024 * 1024), "index.max_file_bytes", minimum=1, integer=True)
    chunk_chars = _validate_number(index.get("chunk_chars", 1200), "index.chunk_chars", minimum=100, integer=True)
    overlap = _validate_number(index.get("chunk_overlap", 150), "index.chunk_overlap", minimum=0, integer=True)
    if overlap >= chunk_chars:
        raise RuntimeError("FileBot setting 'index.chunk_overlap' must be smaller than 'index.chunk_chars'")
    _validate_number(index.get("debounce_seconds", 1.0), "index.debounce_seconds", minimum=0)
    _validate_number(index.get("background_batch_size", 10), "index.background_batch_size", minimum=1, integer=True)
    _validate_number(index.get("background_poll_seconds", 1.0), "index.background_poll_seconds", minimum=0, exclusive=True)
    if not isinstance(settings["runtime"]["hotkey"], str) or not settings["runtime"]["hotkey"].strip():
        raise RuntimeError("FileBot setting 'runtime.hotkey' must be a non-empty string")
    _validate_number(settings["runtime"].get("idle_unload_seconds", 120),
                     "runtime.idle_unload_seconds", minimum=1, integer=True)
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
    embeddings = models["embeddings"]
    _validate_number(embeddings.get("batch_size", 64), "models.embeddings.batch_size", minimum=1, integer=True)
    return settings


def _validate_number(value: Any, name: str, *, minimum: float, integer: bool = False,
                     exclusive: bool = False) -> int | float:
    """Validate a numeric setting before it reaches worker or timer code."""
    valid_type = type(value) is int if integer else isinstance(value, (int, float)) and not isinstance(value, bool)
    operator = ">" if exclusive else ">="
    expected = "integer" if integer else "number"
    if not valid_type:
        raise RuntimeError(f"FileBot setting '{name}' must be a {expected} {operator} {minimum:g}")
    below_minimum = value <= minimum if exclusive else value < minimum
    if below_minimum:
        raise RuntimeError(f"FileBot setting '{name}' must be a {expected} {operator} {minimum:g}")
    return value


def expand_config_path(value: str | Path) -> Path:
    """Expand ``~`` and environment variables in a configured path."""
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    if not expanded.strip():
        raise ValueError("Configured path cannot be empty")
    return Path(expanded)

