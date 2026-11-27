"""Locate bundled read-only resources in a source checkout or a frozen build."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """Return the directory that holds config/, models/ and bin/.

    PyInstaller one-folder builds expose bundled data under ``sys._MEIPASS``
    (the ``_internal`` folder); a source checkout uses the repository root.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """Return an absolute path below :func:`app_root`."""
    return app_root().joinpath(*parts)


def resolve_resource(value: str | Path) -> Path:
    """Expand ``~``/env vars; resolve relative paths against :func:`app_root`."""
    path = Path(os.path.expandvars(str(value))).expanduser()
    return path if path.is_absolute() else app_root() / path


def user_config_dir() -> Path:
    """Writable per-user settings folder: ``%APPDATA%\\FileBot`` on Windows, else ``~/.config/filebot``."""
    appdata = os.environ.get("APPDATA")
    if sys.platform == "win32" and appdata:
        return Path(appdata) / "FileBot"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "filebot"


def user_settings_path() -> Path:
    """Optional user override file merged over the bundled ``config/settings.yaml``."""
    return user_config_dir() / "settings.yaml"
