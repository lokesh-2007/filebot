"""Bundled llama.cpp ``llama-server`` sidecar: loopback-only, fully offline."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.error import HTTPError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)


def _attach_kill_on_close_job(proc: subprocess.Popen[bytes]) -> Any:
    """Windows only: tie the child's lifetime to ours with a kill-on-close Job Object.

    If FileBot is killed or crashes, Windows closes the job handle and ends
    ``llama-server`` too. Returns the job handle (keep it alive) or ``None``.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):  # type: ignore[attr-defined]
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        log.debug("Could not attach llama-server to a kill-on-close job", exc_info=True)
        return None


def _close_job(job: Any) -> None:
    if job is None or os.name != "nt":
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(job)
    except Exception:
        log.debug("Could not close llama-server job handle", exc_info=True)


class LlamaServer:
    """Own one ``llama-server`` child process bound to 127.0.0.1 on a free port."""

    def __init__(self, server_path: Path, model_path: Path, n_ctx: int = 8192,
                 threads: int | None = None, startup_timeout: float = 180.0,
                 log_path: Path | None = None, launcher: Sequence[str] = ()) -> None:
        self.server_path, self.model_path = Path(server_path), Path(model_path)
        self.n_ctx, self.threads, self.startup_timeout = n_ctx, threads, startup_timeout
        self.log_path = Path(log_path) if log_path else None
        # Optional interpreter/wrapper prefix; production leaves it empty. Tests use
        # it to run a fake server script on any platform.
        self.launcher = tuple(launcher)
        self.port: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_file: Any = None
        self._job: Any = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def check_files(self) -> None:
        """Fail early with a clear message when bundled assets are missing."""
        if not self.server_path.is_file():
            raise RuntimeError(f"FileBot's bundled LLM engine is missing: {self.server_path}")
        if not self.model_path.is_file():
            raise RuntimeError(f"FileBot's bundled language model is missing: {self.model_path}")

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _log_tail(self, lines: int = 8) -> str:
        if self.log_path is None:
            return ""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        tail = [line for line in text.splitlines() if line.strip()][-lines:]
        return "\n".join(tail)

    def _startup_error(self, message: str) -> RuntimeError:
        tail = self._log_tail()
        return RuntimeError(f"{message}\nEngine log ({self.log_path}):\n{tail}" if tail else message)

    def start(self) -> None:
        with self._lock:
            if self.running():
                return
            self.check_files()
            self.port = self._free_port()
            command = [*self.launcher, str(self.server_path), "-m", str(self.model_path),
                       "--host", "127.0.0.1", "--port", str(self.port),
                       "-c", str(self.n_ctx), "--jinja", "--no-webui"]
            if self.threads:
                command += ["-t", str(self.threads)]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            output: Any = subprocess.DEVNULL
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_file = self.log_path.open("wb")
                output = self._log_file
            self._proc = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT if self.log_path is not None else subprocess.DEVNULL,
                creationflags=flags,
            )
            self._job = _attach_kill_on_close_job(self._proc)
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    code = self._proc.returncode
                    self._cleanup_locked()
                    raise self._startup_error(f"FileBot's LLM engine exited during startup (code {code}).")
                try:
                    with urlopen(f"{self.base_url}/health", timeout=2) as response:
                        if response.status == 200:
                            return
                except OSError:
                    pass
                time.sleep(0.2)
            self._stop_locked()
            raise self._startup_error("FileBot's LLM engine did not become ready in time.")

    def _cleanup_locked(self) -> None:
        self._proc = None
        _close_job(self._job)
        self._job = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def _stop_locked(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self._cleanup_locked()

    def stop(self) -> None:
        # Deliberately not holding the lock while a start() is polling would race;
        # stop() waits for it, which is bounded by startup_timeout.
        with self._lock:
            self._stop_locked()

    def chat_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                    cancel: threading.Event | None = None, timeout: float = 300.0) -> Iterator[dict[str, Any]]:
        """Yield streamed ``/v1/chat/completions`` chunks; stop quietly on cancel."""
        body: dict[str, Any] = {"messages": messages, "stream": True, "temperature": 0.2}
        if tools:
            body["tools"] = tools
        request = Request(f"{self.base_url}/v1/chat/completions",
                          data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=timeout)
        except HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            raise RuntimeError(f"The local model server rejected the request ({exc.code}): {detail}") from exc
        with response:
            for raw in response:
                if cancel is not None and cancel.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except ValueError:
                    log.debug("Ignoring malformed stream chunk")
