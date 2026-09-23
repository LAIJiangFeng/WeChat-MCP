"""SQLCipher-4 key derivation, HMAC verification and page-wise AES decryption.

This is the foundation of the whole pipeline. Every parameter here matches the
SQLCipher-4 defaults WeChat 4.x uses (verified by pywxdump / chatlog /
touching WeChat, so any later failure is a wrong *key*, not a broken kernel.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct

# --- SQLCipher-4 constants (AES-256-CBC, 4096-byte pages) ---
PAGE_SIZE = 4096
SALT_SZ = 16                     # first 16 plaintext bytes of the file
IV_SZ = 16                       # per-page AES-CBC IV
HMAC_SZ = 64                     # per-page HMAC-SHA512
RESERVE = IV_SZ + HMAC_SZ        # 80 bytes reserved at the end of every page
KEY_SZ = 32                      # 256-bit keys
KDF_ITER = 256000                # PBKDF2 rounds for enc_key
HMAC_KDF_ITER = 2                # PBKDF2 rounds for mac_key
MAC_SALT_XOR = 0x3a
SQLITE_HEADER = b"SQLite format 3\x00"   # 16 bytes, restored onto page 1

assert len(SQLITE_HEADER) == SALT_SZ


def _import_aes():
    from Crypto.Cipher import AES  # pycryptodome
    return AES


def derive_keys(raw_key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """Turn a 32-byte raw key + 16-byte salt into (enc_key, mac_key)."""
    if len(raw_key) != KEY_SZ:
        raise ValueError(f"raw_key must be {KEY_SZ} bytes, got {len(raw_key)}")
    if len(salt) != SALT_SZ:
        raise ValueError(f"salt must be {SALT_SZ} bytes, got {len(salt)}")
    enc_key = hashlib.pbkdf2_hmac("sha512", raw_key, salt, KDF_ITER, KEY_SZ)
    mac_salt = bytes(b ^ MAC_SALT_XOR for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, HMAC_KDF_ITER, KEY_SZ)
    return enc_key, mac_key


def derive_mac_key_from_enc(enc_key: bytes, salt: bytes) -> bytes:
    """When memory already holds the derived enc_key, mac_key is 2 more rounds."""
    mac_salt = bytes(b ^ MAC_SALT_XOR for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, HMAC_KDF_ITER, KEY_SZ)


def _page_hmac(mac_key: bytes, page: bytes, page_no: int, skip: int) -> bytes:
    """HMAC-SHA512 over (cipher-region + IV + little-endian page number).

    ``skip`` drops the leading salt on page 1 (skip=SALT_SZ) and is 0 elsewhere.
    """
    hmac_input_end = PAGE_SIZE - RESERVE + IV_SZ           # cipher region + IV
    body = page[skip:hmac_input_end]
    return hmac.new(mac_key, body + struct.pack("<I", page_no), hashlib.sha512).digest()


def _stored_hmac(page: bytes) -> bytes:
    off = PAGE_SIZE - RESERVE + IV_SZ                      # 4032
    return page[off:off + HMAC_SZ]


def verify_key(first_page: bytes, salt: bytes, raw_key: bytes) -> bool:
    """Recompute page-1 HMAC from a *raw* key candidate and compare (fast check)."""
    _, mac_key = derive_keys(raw_key, salt)
    calc = _page_hmac(mac_key, first_page, 1, SALT_SZ)
    return hmac.compare_digest(_stored_hmac(first_page), calc)


def verify_enc_key(first_page: bytes, salt: bytes, enc_key: bytes) -> bool:
    """Same check but for an already-derived enc_key (only 2 KDF rounds)."""
    mac_key = derive_mac_key_from_enc(enc_key, salt)
    calc = _page_hmac(mac_key, first_page, 1, SALT_SZ)
    return hmac.compare_digest(_stored_hmac(first_page), calc)


def read_first_page(src_path: str) -> bytes:
    with open(src_path, "rb") as fh:
        return fh.read(PAGE_SIZE)


def _decrypt_pages(blob: bytes, enc_key: bytes, mac_key: bytes, dst_path: str) -> int:
    AES = _import_aes()
    if not hmac.compare_digest(_stored_hmac(blob[:PAGE_SIZE]),
                               _page_hmac(mac_key, blob[:PAGE_SIZE], 1, SALT_SZ)):
        raise ValueError("key does not match this database (page-1 HMAC failed)")
    total_pages = len(blob) // PAGE_SIZE
    out = bytearray()
    cipher_end = PAGE_SIZE - RESERVE
    for i in range(total_pages):
        page = blob[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
        data_start = SALT_SZ if i == 0 else 0
        iv = page[cipher_end:cipher_end + IV_SZ]
        cipher_text = page[data_start:cipher_end]
        plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(cipher_text) if cipher_text else b""
        head = SQLITE_HEADER if i == 0 else b""
        out.extend(head + plain + page[cipher_end:])
    tmp = dst_path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(out)
    os.replace(tmp, dst_path)
    return total_pages


def decrypt_db_with_enc_key(src_path: str, enc_key, dst_path: str) -> int:
    """Decrypt using an already-derived enc_key (mac_key re-derived from salt)."""
    if isinstance(enc_key, str):
        enc_key = bytes.fromhex(enc_key.strip())
    with open(src_path, "rb") as fh:
        blob = fh.read()
    if len(blob) < PAGE_SIZE:
        raise ValueError(f"{src_path} smaller than one page")
    salt = blob[:SALT_SZ]
    mac_key = derive_mac_key_from_enc(enc_key, salt)
    return _decrypt_pages(blob, enc_key, mac_key, dst_path)


def decrypt_db(src_path: str, hex_or_raw_key, dst_path: str) -> int:
    """Decrypt an encrypted SQLCipher-4 DB to a plaintext SQLite file.

    ``hex_or_raw_key`` may be a 64-char hex string or 32 raw bytes.
    Returns the number of pages written.
    """
    AES = _import_aes()
    if isinstance(hex_or_raw_key, str):
        raw_key = bytes.fromhex(hex_or_raw_key.strip())
    else:
        raw_key = bytes(hex_or_raw_key)

    with open(src_path, "rb") as fh:
        blob = fh.read()
    if len(blob) < PAGE_SIZE:
        raise ValueError(f"{src_path} smaller than one page")

    salt = blob[:SALT_SZ]
    enc_key, mac_key = derive_keys(raw_key, salt)

    # Fail fast if the key is wrong, before writing anything.
    if not hmac.compare_digest(_stored_hmac(blob[:PAGE_SIZE]),
                               _page_hmac(mac_key, blob[:PAGE_SIZE], 1, SALT_SZ)):
        raise ValueError("key does not match this database (page-1 HMAC failed)")

    total_pages = len(blob) // PAGE_SIZE
    out = bytearray()
    cipher_end = PAGE_SIZE - RESERVE          # 4016
    iv_start = cipher_end                      # 4016
    for i in range(total_pages):
        page = blob[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
        data_start = SALT_SZ if i == 0 else 0
        iv = page[iv_start:iv_start + IV_SZ]
        cipher_text = page[data_start:cipher_end]
        if cipher_text:
            plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(cipher_text)
        else:
            plain = b""
        head = SQLITE_HEADER if i == 0 else b""
        out.extend(head + plain + page[cipher_end:])   # keep IV+HMAC reserve

    tmp = dst_path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(out)
    os.replace(tmp, dst_path)
    return total_pages


def decrypt_db_stream(src_path: str, hex_or_raw_key, dst_path: str) -> int:
    """Same result as :func:`decrypt_db`, but streams page by page.

    Memory stays at one page instead of two copies of the whole database, which
    matters for the long-running refresh daemon.
    """
    AES = _import_aes()
    if isinstance(hex_or_raw_key, str):
        raw_key = bytes.fromhex(hex_or_raw_key.strip())
    else:
        raw_key = bytes(hex_or_raw_key)

    size = os.path.getsize(src_path)
    if size < PAGE_SIZE:
        raise ValueError(f"{src_path} smaller than one page")
    total_pages = size // PAGE_SIZE
    cipher_end = PAGE_SIZE - RESERVE

    with open(src_path, "rb") as src:
        first = src.read(PAGE_SIZE)
        salt = first[:SALT_SZ]
        enc_key, mac_key = derive_keys(raw_key, salt)
        if not hmac.compare_digest(_stored_hmac(first),
                                   _page_hmac(mac_key, first, 1, SALT_SZ)):
            raise ValueError("key does not match this database (page-1 HMAC failed)")

        tmp = dst_path + ".part"
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        with open(tmp, "wb") as out:
            page = first
            for i in range(total_pages):
                if i:
                    page = src.read(PAGE_SIZE)
                data_start = SALT_SZ if i == 0 else 0
                iv = page[cipher_end:cipher_end + IV_SZ]
                cipher_text = page[data_start:cipher_end]
                plain = (AES.new(enc_key, AES.MODE_CBC, iv).decrypt(cipher_text)
                         if cipher_text else b"")
                if i == 0:
                    out.write(SQLITE_HEADER)
                out.write(plain)
                out.write(page[cipher_end:])
    os.replace(tmp, dst_path)
    return total_pages


def decrypt_db_diff(src_path: str, hex_or_raw_key, dst_path: str) -> tuple[int, int]:
    """Refresh an existing plaintext file in place, writing only changed pages.

    WeChat appends to its databases, so between two refreshes almost every page
    is identical.  Rewriting the whole file would cost tens of GB of disk writes
    per day for a long-running daemon; this writes only the pages that differ.

    Falls back to a full streaming decrypt when no usable destination exists.
    Returns ``(total_pages, pages_written)``.
    """
    if not os.path.exists(dst_path):
        return decrypt_db_stream(src_path, hex_or_raw_key, dst_path), -1

    AES = _import_aes()
    if isinstance(hex_or_raw_key, str):
        raw_key = bytes.fromhex(hex_or_raw_key.strip())
    else:
        raw_key = bytes(hex_or_raw_key)

    size = os.path.getsize(src_path)
    if size < PAGE_SIZE:
        raise ValueError(f"{src_path} smaller than one page")
    total_pages = size // PAGE_SIZE
    cipher_end = PAGE_SIZE - RESERVE
    written = 0

    with open(src_path, "rb") as src:
        first = src.read(PAGE_SIZE)
        salt = first[:SALT_SZ]
        enc_key, mac_key = derive_keys(raw_key, salt)
        if not hmac.compare_digest(_stored_hmac(first),
                                   _page_hmac(mac_key, first, 1, SALT_SZ)):
            raise ValueError("key does not match this database (page-1 HMAC failed)")

        with open(dst_path, "r+b") as dst:
            page = first
            for i in range(total_pages):
                if i:
                    page = src.read(PAGE_SIZE)
                data_start = SALT_SZ if i == 0 else 0
                iv = page[cipher_end:cipher_end + IV_SZ]
                cipher_text = page[data_start:cipher_end]
                plain = (AES.new(enc_key, AES.MODE_CBC, iv).decrypt(cipher_text)
                         if cipher_text else b"")
                out_page = (SQLITE_HEADER if i == 0 else b"") + plain + page[cipher_end:]

                offset = i * PAGE_SIZE
                dst.seek(offset)
                if dst.read(PAGE_SIZE) != out_page:
                    dst.seek(offset)
                    dst.write(out_page)
                    written += 1
            dst.truncate(total_pages * PAGE_SIZE)
    return total_pages, written
