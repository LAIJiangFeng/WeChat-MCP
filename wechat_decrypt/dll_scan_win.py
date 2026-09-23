"""Extract the internal_db_key(s) compiled into Weixin.dll.

WeChat 4.x XORs the raw in-memory key with a 32-byte constant baked into the
DLL. The constant appears as four consecutive ``mov rdx, imm64`` (48 BA ..)
instructions followed by ``test rax, rax`` (48 85 C0). We scan the executable
sections (via pefile when available, else the whole file) for that shape and
concatenate the four 8-byte immediates into a 32-byte candidate.
"""
from __future__ import annotations

import glob
import os
import re
import winreg

# 4x (48 BA <imm64>) with 3..8 filler bytes between, then 48 85 C0.
_PATTERN = re.compile(
    rb"\x48\xBA(.{8})"
    rb".{0,8}?\x48\xBA(.{8})"
    rb".{0,8}?\x48\xBA(.{8})"
    rb".{0,8}?\x48\xBA(.{8})"
    rb".{0,8}?\x48\x85\xC0",
    re.DOTALL,
)


def _install_path_from_registry() -> str | None:
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Tencent\Weixin") as k:
                val, _ = winreg.QueryValueEx(k, "InstallPath")
                if val:
                    return val
        except OSError:
            continue
    return None


def resolve_weixin_dll(explicit: str | None = None) -> str:
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise FileNotFoundError(f"指定的 DLL 不存在: {explicit}")

    roots = []
    reg = _install_path_from_registry()
    if reg:
        roots.append(reg)
    # Vendor defaults on every existing drive; WeChat is often not on C:.
    for drive in (f"{chr(c)}:" for c in range(ord("A"), ord("Z") + 1)):
        if not os.path.isdir(drive + os.sep):
            continue
        roots += [
            os.path.join(drive + os.sep, "Program Files", "Tencent", "Weixin"),
            os.path.join(drive + os.sep, "Program Files (x86)", "Tencent", "Weixin"),
            os.path.join(drive + os.sep, "Software", "WeChat", "Weixin"),
        ]
    candidates: list[str] = []
    for root in roots:
        candidates += glob.glob(os.path.join(root, "Weixin.dll"))
        candidates += glob.glob(os.path.join(root, "*", "Weixin.dll"))
    # newest version directory first
    candidates = sorted(set(candidates), reverse=True)
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError(
        "找不到 Weixin.dll；用 --dll 手动指定，或用 --internal-key 直接传 64 位 hex")


def _exec_section_bytes(dll_path: str) -> list[bytes]:
    """Prefer executable sections via pefile; fall back to the whole file."""
    try:
        import pefile
    except Exception:
        with open(dll_path, "rb") as fh:
            return [fh.read()]
    chunks: list[bytes] = []
    pe = pefile.PE(dll_path, fast_load=True)
    IMAGE_SCN_MEM_EXECUTE = 0x20000000
    for sec in pe.sections:
        if sec.Characteristics & IMAGE_SCN_MEM_EXECUTE:
            chunks.append(sec.get_data())
    pe.close()
    if not chunks:
        with open(dll_path, "rb") as fh:
            return [fh.read()]
    return chunks


def extract_internal_keys(dll_path: str) -> list[bytes]:
    """Return the distinct 32-byte internal_db_key candidates found in the DLL."""
    seen: set[bytes] = set()
    ordered: list[bytes] = []
    for blob in _exec_section_bytes(dll_path):
        for m in _PATTERN.finditer(blob):
            key = b"".join(m.group(i) for i in range(1, 5))
            if len(key) == 32 and key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered
