"""Cache verified database keys, protected by Windows DPAPI.

The key is the SQLCipher key the database was encrypted with, so it is stable
until the account is re-created.  Caching it turns a multi-minute memory scan
into a 3-second decrypt.

DPAPI ties the ciphertext to the current Windows user account: copying the file
to another machine or another user makes it useless.  The store deliberately
lives outside the repository so it can never be committed.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os

STORE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "wechat-mcp"
)
STORE_PATH = os.path.join(STORE_DIR, "dbkeys.dpapi")

_ENTROPY = b"wechat-mcp/dbkeys/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _take(blob: _Blob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(data: bytes) -> bytes:
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_blob(data)), "wechat-mcp db keys",
        ctypes.byref(_blob(_ENTROPY)), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.GetLastError()}")
    return _take(out)


def unprotect(data: bytes) -> bytes:
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_blob(data)), None,
        ctypes.byref(_blob(_ENTROPY)), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
    return _take(out)


def load() -> dict[str, dict[str, str]]:
    """Return ``{wxid: {"key": hex, "method": ...}}``; empty when unreadable."""
    try:
        with open(STORE_PATH, "rb") as fh:
            return json.loads(unprotect(fh.read()).decode("utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}


def save(entries: dict[str, dict[str, str]]) -> str:
    os.makedirs(STORE_DIR, exist_ok=True)
    payload = protect(json.dumps(entries, ensure_ascii=False).encode("utf-8"))
    tmp = STORE_PATH + ".part"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, STORE_PATH)
    return STORE_PATH


def forget() -> bool:
    """Delete the cached keys; returns True when a store was removed."""
    try:
        os.remove(STORE_PATH)
        return True
    except FileNotFoundError:
        return False
