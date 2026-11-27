"""Validated FileBot YAML settings and Windows-friendly path expansion."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any


def _merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``: mappings merge, everything else replaces."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(path: str | Path, user_path: str | Path | None = None) -> dict[str, Any]:
    """Load settings and reject malformed top-level sections with a useful error.

    ``user_path`` is an optional writable override file (see
    ``resources.user_settings_path``); when it exists its values are merged over
    the bundled defaults, so installed builds never edit files in the app folder.
    """
    import yaml

    source = Path(path)
    try:
        settings = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Cannot load FileBot settings from {source}: {exc}") from exc
    if not isinstance(settings, dict):
        raise RuntimeError(f"FileBot settings must contain a YAML mapping: {source}")
    if user_path is not None and Path(user_path).is_file():
        try:
            override = yaml.safe_load(Path(user_path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(f"Cannot load your FileBot settings from {user_path}: {exc}") from exc
        if override is not None:
            if not isinstance(override, dict):
                raise RuntimeError(f"Your FileBot settings must be a YAML mapping: {user_path}")
            settings = _merge_settings(settings, override)
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
    for key in ("exclude_paths", "exclude_globs"):
        value = index.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise RuntimeError(f"FileBot setting 'index.{key}' must be a list of non-empty strings")
    if not isinstance(index.get("scan_fixed_drives", False), bool):
        raise RuntimeError("FileBot setting 'index.scan_fixed_drives' must be true or false")
    _validate_number(index.get("rescan_seconds", 3600), "index.rescan_seconds", minimum=0)
    _validate_number(index.get("max_pending_events", 50000), "index.max_pending_events", minimum=100, integer=True)
    _validate_number(index.get("max_file_bytes", 50 * 1024 * 1024), "index.max_file_bytes", minimum=1, integer=True)
    _validate_number(index.get("max_extracted_chars", 5_000_000),
                     "index.max_extracted_chars", minimum=1, integer=True)
    _validate_number(index.get("max_docx_uncompressed_bytes", 100_000_000),
                     "index.max_docx_uncompressed_bytes", minimum=1, integer=True)
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
    for section in ("llm", "embeddings", "vector_store"):
        if not isinstance(models.get(section), dict):
            raise RuntimeError(f"FileBot settings section 'models.{section}' must be a mapping")
    for section, required in {
        "llm": ("server_path", "model_path"),
        "embeddings": ("model", "cache_dir"),
        "vector_store": ("path",),
    }.items():
        missing = [key for key in required if key not in models[section]]
        if missing:
            raise RuntimeError(f"FileBot settings section 'models.{section}' is missing: {', '.join(missing)}")
    llm = models["llm"]
    for key in ("server_path", "model_path"):
        if not isinstance(llm[key], str) or not llm[key].strip():
            raise RuntimeError(f"FileBot setting 'models.llm.{key}' must be a non-empty path")
    _validate_number(llm.get("n_ctx", 8192), "models.llm.n_ctx", minimum=512, integer=True)
    if llm.get("threads") is not None:
        _validate_number(llm["threads"], "models.llm.threads", minimum=1, integer=True)
    embeddings = models["embeddings"]
    if embeddings.get("model_dir") is not None and (
        not isinstance(embeddings["model_dir"], str) or not embeddings["model_dir"].strip()
    ):
        raise RuntimeError("FileBot setting 'models.embeddings.model_dir' must be a non-empty path")
    _validate_number(embeddings.get("batch_size", 64), "models.embeddings.batch_size", minimum=1, integer=True)
    if index.get("scan_fixed_drives"):
        known = {os.path.normcase(str(expand_config_path(root))).rstrip("\\/") for root in index["roots"]}
        for drive in fixed_drive_roots():
            if os.path.normcase(drive).rstrip("\\/") not in known:
                index["roots"].append(drive)
    return settings


def fixed_drive_roots() -> list[str]:
    """Return fixed local drive roots such as ``C:\\`` on Windows; empty elsewhere."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        mask = kernel32.GetLogicalDrives()
        drives = []
        for index in range(26):
            root = f"{chr(65 + index)}:\\"
            if mask & (1 << index) and kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                drives.append(root)
        return drives
    except Exception:
        return []


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


def _norm(path: str | Path) -> str:
    """Case-normalized absolute path without touching the filesystem."""
    return os.path.normcase(os.path.abspath(str(path)))


class IndexScope:
    """Index roots plus exclusion rules, resolved once instead of per filesystem event.

    Roots are resolved a single time (following symlinks) so containment checks
    only resolve the candidate. Exclusions come from directory names
    (``exclude_dirs``), name globs (``exclude_globs``) and absolute path
    prefixes (``exclude_paths``, with ``%VAR%``/``~`` expansion; entries whose
    variables are undefined on this machine are ignored).
    """

    def __init__(self, roots: list[str | Path], exclude_names: list[str] | None = None,
                 exclude_paths: list[str] | None = None, exclude_globs: list[str] | None = None) -> None:
        resolved: list[Path] = []
        for root in roots:
            try:
                resolved.append(expand_config_path(root).resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        self.roots = resolved
        self.names = {name.lower() for name in (exclude_names or [])}
        self.globs = [pattern.lower() for pattern in (exclude_globs or [])]
        self.path_prefixes: list[str] = []
        for entry in exclude_paths or []:
            expanded = os.path.expandvars(os.path.expanduser(entry))
            if "%" in expanded or "$" in expanded:  # undefined variable: not applicable here
                continue
            self.path_prefixes.append(_norm(expanded).rstrip("\\/"))

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "IndexScope":
        index = settings.get("index", {})
        return cls(index.get("roots", []), index.get("exclude_dirs", []),
                   index.get("exclude_paths", []), index.get("exclude_globs", []))

    def prefixes(self) -> list[str]:
        """Root directories as SQL path prefixes, each ending in a separator."""
        return [str(root).rstrip("\\/") + os.sep for root in self.roots]

    def contains(self, path: str | Path) -> bool:
        """Whether the resolved path lies under a root; invalid paths fail closed."""
        try:
            candidate = Path(path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        for root in self.roots:
            try:
                candidate.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def name_excluded(self, name: str) -> bool:
        lowered = name.lower()
        return lowered in self.names or any(fnmatch.fnmatchcase(lowered, pattern) for pattern in self.globs)

    def excluded(self, path: str | Path) -> bool:
        """Whether any path component or prefix is excluded (no filesystem access)."""
        target = Path(path)
        if any(self.name_excluded(part) for part in target.parts):
            return True
        normalized = _norm(target)
        return any(normalized == prefix or normalized.startswith(prefix + os.sep)
                   for prefix in self.path_prefixes)

    def dir_excluded(self, name: str, path: str | Path) -> bool:
        """Whether a child directory is excluded; ancestors were already checked by the walk."""
        if self.name_excluded(name):
            return True
        if not self.path_prefixes:
            return False
        normalized = _norm(path)
        return any(normalized == prefix or normalized.startswith(prefix + os.sep)
                   for prefix in self.path_prefixes)

    def walk_filter(self, directory: str, dirs: list[str]) -> list[str]:
        """Prune excluded child directories during ``os.walk`` (cheap: names + prefixes)."""
        return [name for name in dirs
                if not self.name_excluded(name) and not self.excluded(os.path.join(directory, name))]
