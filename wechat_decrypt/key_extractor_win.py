"""Recover the WeChat 4.x database key from process memory and verify it.

Method (clean-room from the documented algorithm, same as pywxdump / chatlog /
wechat-dump-rs):

1. The key is referenced by a std::string-like structure on a private heap:
   [ptr(8)][SSO buffer ...][size_t size == 0x20][size_t capacity]. We find the
   size==32 marker with a sane capacity, step back to the pointer, and read the
   32 bytes it points at -> a *raw* key candidate.
2. The real key is either that raw value, or raw XOR internal_db_key (from the
   DLL), or (occasionally) the already-derived enc_key.
3. Every candidate is checked against the target DB's page-1 HMAC, so a wrong
   candidate can never be accepted -- verification is exact.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from wechat_decrypt import mem_windows as mem
from wechat_decrypt import sqlcipher4 as sc

_SIZE_MARKER = struct.pack("<Q", sc.KEY_SZ)         # size_t == 32
_MIN_CAP = sc.KEY_SZ                                 # capacity >= 32
_MAX_CAP = 0x1000
_USER_MIN = 0x10000                                  # plausible userspace ptr
_USER_MAX = 0x7FFFFFFFFFFF


@dataclass
class FoundKey:
    hex_key: str
    method: str          # "raw", "xor", or "enc"


def _low_entropy(buf: bytes) -> bool:
    if len(buf) < sc.KEY_SZ:
        return True
    if buf == b"\x00" * len(buf):
        return True
    if len(set(buf)) < 6:                            # too few distinct bytes
        return True
    return False


def scan_raw_key_candidates(handle: int, *, limit: int = 4000) -> list[bytes]:
    """Return distinct 32-byte raw key candidates pointed at by string structs."""
    seen: set[bytes] = set()
    ordered: list[bytes] = []
    for region in mem.enum_regions(handle, writable_only=True):
        blob = mem.read_region(handle, region.base, region.size)
        if not blob:
            continue
        start = 0
        while True:
            idx = blob.find(_SIZE_MARKER, start)
            if idx < 0:
                break
            start = idx + 1
            # capacity right after size
            cap_off = idx + 8
            if cap_off + 8 > len(blob):
                continue
            cap = struct.unpack_from("<Q", blob, cap_off)[0]
            if not (_MIN_CAP <= cap <= _MAX_CAP):
                continue
            # pointer sits 16 bytes before the size field (SSO union)
            ptr_off = idx - 16
            if ptr_off < 0:
                continue
            ptr = struct.unpack_from("<Q", blob, ptr_off)[0]
            if not (_USER_MIN <= ptr <= _USER_MAX):
                continue
            cand = mem.read_region(handle, ptr, sc.KEY_SZ)
            if not cand or _low_entropy(cand):
                continue
            if cand not in seen:
                seen.add(cand)
                ordered.append(cand)
                if len(ordered) >= limit:
                    return ordered
    return ordered


def _verify_candidate(cand: bytes, internal_keys: list[bytes],
                      first_page: bytes, salt: bytes) -> FoundKey | None:
    if sc.verify_key(first_page, salt, cand):
        return FoundKey(cand.hex(), "raw")
    for ik in internal_keys:
        xored = bytes(a ^ b for a, b in zip(cand, ik))
        if sc.verify_key(first_page, salt, xored):
            return FoundKey(xored.hex(), "xor")
    if sc.verify_enc_key(first_page, salt, cand):
        # memory already held the derived enc_key; store it as-is (hex),
        # decrypt path re-derives, so we must mark it. We can't feed enc_key to
        # decrypt_db (it derives again), so only report -- handled by caller.
        return FoundKey(cand.hex(), "enc")
    return None


def find_key_for_db(handle: int, candidates: list[bytes],
                    internal_keys: list[bytes], db_path: str) -> FoundKey | None:
    first = sc.read_first_page(db_path)
    salt = first[:sc.SALT_SZ]
    for cand in candidates:
        found = _verify_candidate(cand, internal_keys, first, salt)
        if found and found.method != "enc":
            return found
    # fall back to reporting an enc-only match if that's all we have
    for cand in candidates:
        found = _verify_candidate(cand, internal_keys, first, salt)
        if found:
            return found
    return None
