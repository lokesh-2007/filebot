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
    retrieval = settings.get("retrieval", {})
    if not isinstance(retrieval, dict):
        raise RuntimeError("FileBot settings section 'retrieval' must be a mapping")
    glossary = retrieval.get("glossary", {})
    if not isinstance(glossary, dict) or any(
        not isinstance(alias, str) or not alias.strip() or len(alias) > 100
        or not isinstance(expansion, str) or not expansion.strip() or len(expansion) > 500
        for alias, expansion in glossary.items()
    ):
        raise RuntimeError("FileBot setting 'retrieval.glossary' must map short aliases to non-empty expansions")
    max_in_memory_chunks = _validate_number(
        retrieval.get("max_in_memory_chunks", 10_000),
        "retrieval.max_in_memory_chunks", minimum=1, integer=True,
    )
    if max_in_memory_chunks > 10_000:
        raise RuntimeError("FileBot setting 'retrieval.max_in_memory_chunks' must be at most 10000")
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
    _validate_number(index.get("max_extracted_chars", 5_000_000),
                     "index.max_extracted_chars", minimum=1, integer=True)
    _validate_number(index.get("max_docx_uncompressed_bytes", 100_000_000),
                     "index.max_docx_uncompressed_bytes", minimum=1, integer=True)
    source_timeout = _validate_number(
        index.get("source_parse_timeout_ms", 250),
        "index.source_parse_timeout_ms", minimum=1, integer=True,
    )
    if source_timeout > 5_000:
        raise RuntimeError("FileBot setting 'index.source_parse_timeout_ms' must be at most 5000")
    request_timeout = _validate_number(
        settings["runtime"].get("request_timeout_seconds", 180),
        "runtime.request_timeout_seconds", minimum=15, integer=True,
    )
    if request_timeout > 600:
        raise RuntimeError("FileBot setting 'runtime.request_timeout_seconds' must be at most 600")
    chunk_chars = _validate_number(index.get("chunk_chars", 1200), "index.chunk_chars", minimum=100, integer=True)
    overlap = _validate_number(index.get("chunk_overlap", 150), "index.chunk_overlap", minimum=0, integer=True)
    if overlap >= chunk_chars:
        raise RuntimeError("FileBot setting 'index.chunk_overlap' must be smaller than 'index.chunk_chars'")
    _validate_number(index.get("debounce_seconds", 1.0), "index.debounce_seconds", minimum=0)
    background_batch_size = _validate_number(
        index.get("background_batch_size", 10), "index.background_batch_size", minimum=1, integer=True,
    )
    if background_batch_size > 10:
        raise RuntimeError("FileBot setting 'index.background_batch_size' must be at most 10")
    _validate_number(index.get("background_poll_seconds", 1.0), "index.background_poll_seconds", minimum=0, exclusive=True)
    if not isinstance(settings["runtime"]["hotkey"], str) or not settings["runtime"]["hotkey"].strip():
        raise RuntimeError("FileBot setting 'runtime.hotkey' must be a non-empty string")
    _validate_number(settings["runtime"].get("idle_unload_seconds", 120),
                     "runtime.idle_unload_seconds", minimum=1, integer=True)
    memory_budget = _validate_number(
        settings["runtime"].get("managed_model_memory_budget_mib", 4096),
        "runtime.managed_model_memory_budget_mib", minimum=1024, integer=True,
    )
    if memory_budget > 16384:
        raise RuntimeError("FileBot setting 'runtime.managed_model_memory_budget_mib' must be at most 16384")
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
    embedding_batch_size = _validate_number(
        embeddings.get("batch_size", 8), "models.embeddings.batch_size", minimum=1, integer=True,
    )
    if embedding_batch_size > 16:
        raise RuntimeError("FileBot setting 'models.embeddings.batch_size' must be at most 16 for bounded memory use")
    ollama = models["ollama"]
    if "auto_start" in ollama and type(ollama["auto_start"]) is not bool:
        raise RuntimeError("FileBot setting 'models.ollama.auto_start' must be true or false")
    _validate_number(
        ollama.get("startup_timeout_seconds", 30),
        "models.ollama.startup_timeout_seconds",
        minimum=1,
        integer=True,
    )
    num_ctx = _validate_number(
        ollama.get("num_ctx", 4096), "models.ollama.num_ctx", minimum=512, integer=True,
    )
    if num_ctx > 4096:
        raise RuntimeError("FileBot setting 'models.ollama.num_ctx' must be at most 4096 for the supported memory profile")
    num_predict = _validate_number(
        ollama.get("num_predict", 256), "models.ollama.num_predict", minimum=1, integer=True,
    )
    if num_predict > num_ctx:
        raise RuntimeError("FileBot setting 'models.ollama.num_predict' cannot exceed models.ollama.num_ctx")
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
    path = Path(expanded)
    # Windows often redirects the known Documents folder into OneDrive while
    # keeping ``%USERPROFILE%\\Documents`` as a separate legacy directory.
    # Honor the configured ~/Documents root through that redirection when it
    # exists, so FileBot opens and indexes the folder Windows actually uses.
    normalized = str(value).replace("\\", "/").casefold()
    one_drive = os.environ.get("OneDrive") or os.environ.get("OneDriveCommercial")
    if os.name == "nt" and normalized == "~/documents" and one_drive:
        redirected = Path(one_drive) / "Documents"
        if redirected.is_dir():
            return redirected
    return path


def path_is_within_roots(path: str | Path, roots: list[str | Path]) -> bool:
    """Return whether a resolved path is contained by any configured root.

    Resolution follows existing symlinks, preventing an in-root link from
    silently extending the index or file-open scope to another directory.
    Invalid paths and symlink loops fail closed.
    """
    try:
        candidate = Path(path).expanduser().resolve()
        for configured_root in roots:
            root = Path(configured_root).expanduser().resolve()
            try:
                candidate.relative_to(root)
                return True
            except ValueError:
                continue
    except (OSError, RuntimeError, ValueError):
        return False
    return False
