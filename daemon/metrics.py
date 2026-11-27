"""Small opt-in process measurements for FileBot's Windows lifecycle logs."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class _ProcessMemoryCounters(ctypes.Structure):
    """Windows PROCESS_MEMORY_COUNTERS layout used by psapi."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_working_set_mb() -> float | None:
    """Return this process's current Windows working set in MiB if available.

    Unsupported platforms and API failures return ``None`` so diagnostics can
    never prevent FileBot from launching, waking, or sleeping.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_memory_info = psapi.GetProcessMemoryInfo
        get_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_memory_info.restype = wintypes.BOOL
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not get_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
            return None
        return float(counters.WorkingSetSize) / (1024 * 1024)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def process_memory_snapshot_mib() -> dict[str, float] | None:
    """Measure FileBot and Ollama process private commit and resident working set.

    The Ollama group includes processes whose executable name starts with
    ``ollama`` (server and model runners). Shared working-set pages can be
    counted in more than one process, so private commit and working set are
    reported separately. Permission-denied or exited processes are skipped.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        enum_processes = psapi.EnumProcesses
        enum_processes.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD)]
        enum_processes.restype = wintypes.BOOL
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        query_image = kernel32.QueryFullProcessImageNameW
        query_image.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                ctypes.POINTER(wintypes.WCHAR),
                                ctypes.POINTER(wintypes.DWORD)]
        query_image.restype = wintypes.BOOL
        get_memory_info = psapi.GetProcessMemoryInfo
        get_memory_info.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD]
        get_memory_info.restype = wintypes.BOOL

        capacity = 2048
        while capacity <= 65536:
            process_ids = (wintypes.DWORD * capacity)()
            bytes_needed = wintypes.DWORD()
            if not enum_processes(process_ids, ctypes.sizeof(process_ids),
                                 ctypes.byref(bytes_needed)):
                return None
            if bytes_needed.value < ctypes.sizeof(process_ids):
                break
            capacity *= 2
        else:
            return None

        filebot_working = filebot_private = 0
        ollama_working = ollama_private = 0
        ollama_count = 0
        rights = 0x0400 | 0x0010  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        mib = 1024 * 1024
        for pid in process_ids[:bytes_needed.value // ctypes.sizeof(wintypes.DWORD)]:
            if not pid:
                continue
            handle = open_process(rights, False, pid)
            if not handle:
                continue
            try:
                image_buffer = ctypes.create_unicode_buffer(1024)
                image_length = wintypes.DWORD(len(image_buffer))
                if not query_image(handle, 0, image_buffer, ctypes.byref(image_length)):
                    continue
                name = os.path.basename(image_buffer.value).casefold()
                is_filebot = int(pid) == os.getpid()
                is_ollama = name.startswith("ollama")
                if not (is_filebot or is_ollama):
                    continue
                counters = _ProcessMemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if not get_memory_info(handle, ctypes.byref(counters), counters.cb):
                    continue
                if is_filebot:
                    filebot_working += counters.WorkingSetSize
                    filebot_private += counters.PagefileUsage
                if is_ollama:
                    ollama_working += counters.WorkingSetSize
                    ollama_private += counters.PagefileUsage
                    ollama_count += 1
            finally:
                close_handle(handle)
        return {
            "filebot_working_set_mib": filebot_working / mib,
            "filebot_private_commit_mib": filebot_private / mib,
            "ollama_working_set_mib": ollama_working / mib,
            "ollama_private_commit_mib": ollama_private / mib,
            "ollama_process_count": float(ollama_count),
        }
    except (AttributeError, OSError, TypeError, ValueError):
        return None


class _MemoryStatusEx(ctypes.Structure):
    """Windows GlobalMemoryStatusEx layout."""

    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def system_memory_snapshot_mib() -> dict[str, float] | None:
    """Return total and available physical/system-commit memory on Windows."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        global_memory_status = kernel32.GlobalMemoryStatusEx
        global_memory_status.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        global_memory_status.restype = wintypes.BOOL
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not global_memory_status(ctypes.byref(status)):
            return None
        mib = 1024 * 1024
        return {
            "physical_total_mib": status.ullTotalPhys / mib,
            "physical_available_mib": status.ullAvailPhys / mib,
            "commit_total_mib": status.ullTotalPageFile / mib,
            "commit_available_mib": status.ullAvailPageFile / mib,
        }
    except (AttributeError, OSError, TypeError, ValueError):
        return None
