"""Read another process's memory on Windows via kernel32 (pure ctypes).

Windows has no /proc/<pid>/mem, so we use OpenProcess + VirtualQueryEx +
ReadProcessMemory. We only keep committed, readable, private regions (the key
lives in a private heap), and skip oversized regions.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import psutil

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000

PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_READONLY = 0x02
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

_READABLE = (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_READONLY
             | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)

# The raw key sits in a private heap; scanning every readonly/image page as well
# is slow and unnecessary. Default to writable private regions.
_KEY_PROTECT = (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE
                | PAGE_EXECUTE_WRITECOPY)

MAX_REGION = 512 * 1024 * 1024      # skip absurdly large mappings


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]


kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]


@dataclass
class Region:
    base: int
    size: int
    protect: int


def find_weixin_pid() -> int:
    """The main Weixin.exe holds the DB handles and the key: pick the one with
    the largest private working set (mini-program hosts WeChatAppEx.exe don't)."""
    best_pid, best_priv = None, -1
    for p in psutil.process_iter(["name", "pid"]):
        if (p.info["name"] or "").lower() != "weixin.exe":
            continue
        try:
            priv = p.memory_info().private
        except Exception:
            continue
        if priv > best_priv:
            best_priv, best_pid = priv, p.info["pid"]
    if best_pid is None:
        raise RuntimeError("找不到运行中的 Weixin.exe（请先登录微信）")
    return best_pid


def open_process(pid: int) -> int:
    handle = kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        raise PermissionError(
            f"OpenProcess(pid={pid}) 失败 (err={err})；请用管理员 PowerShell 重试")
    return handle


def close_process(handle: int) -> None:
    if handle:
        kernel32.CloseHandle(handle)


def enum_regions(handle: int, *, writable_only: bool = True) -> list[Region]:
    regions: list[Region] = []
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION()
    size = ctypes.sizeof(mbi)
    want = _KEY_PROTECT if writable_only else _READABLE
    while addr < 0x7FFFFFFFFFFF:
        got = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), size)
        if not got:
            break
        base = mbi.BaseAddress or 0
        region = mbi.RegionSize or 0
        if region == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE
                and (mbi.Protect & want)
                and not (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS))
                and region <= MAX_REGION):
            regions.append(Region(base, region, mbi.Protect))
        addr = base + region
    return regions


def read_region(handle: int, base: int, size: int) -> bytes | None:
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base), buf, size,
                                    ctypes.byref(read))
    if not ok or read.value == 0:
        return None
    return buf.raw[:read.value]
