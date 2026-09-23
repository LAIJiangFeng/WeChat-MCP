"""Unit tests for the SQLCipher-4 crypto core (no WeChat required)."""
import hashlib
import hmac
import os
import struct
import unittest

from wechat_decrypt import sqlcipher4 as sc


def build_encrypted(raw_key: bytes, n_pages: int = 4):
    from Crypto.Cipher import AES
    salt = os.urandom(sc.SALT_SZ)
    enc_key, mac_key = sc.derive_keys(raw_key, salt)
    cipher_end = sc.PAGE_SIZE - sc.RESERVE
    out = bytearray()
    contents = []
    for i in range(n_pages):
        clen = cipher_end - (sc.SALT_SZ if i == 0 else 0)
        content = os.urandom(clen)
        contents.append(content)
        iv = os.urandom(sc.IV_SZ)
        ct = AES.new(enc_key, AES.MODE_CBC, iv).encrypt(content)
        prefix = salt if i == 0 else b""
        body = prefix + ct + iv
        skip = sc.SALT_SZ if i == 0 else 0
        mac = hmac.new(mac_key, body[skip:] + struct.pack("<I", i + 1),
                       hashlib.sha512).digest()
        out.extend(body + mac)
    return bytes(out), salt, contents


class CryptoCoreTest(unittest.TestCase):
    def setUp(self):
        self.raw_key = os.urandom(sc.KEY_SZ)
        self.blob, self.salt, self.contents = build_encrypted(self.raw_key)
        self.tmp = os.path.join(os.environ.get("TEMP", "."), f"wxsc_{os.getpid()}.db")
        with open(self.tmp, "wb") as fh:
            fh.write(self.blob)
        self.out = self.tmp + ".plain"

    def tearDown(self):
        for p in (self.tmp, self.out, self.out + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_derive_keys_lengths(self):
        enc, mac = sc.derive_keys(self.raw_key, self.salt)
        self.assertEqual(len(enc), 32)
        self.assertEqual(len(mac), 32)

    def test_verify_key_true_and_false(self):
        first = self.blob[:sc.PAGE_SIZE]
        self.assertTrue(sc.verify_key(first, self.salt, self.raw_key))
        self.assertFalse(sc.verify_key(first, self.salt, os.urandom(sc.KEY_SZ)))

    def test_verify_enc_key(self):
        first = self.blob[:sc.PAGE_SIZE]
        enc, _ = sc.derive_keys(self.raw_key, self.salt)
        self.assertTrue(sc.verify_enc_key(first, self.salt, enc))
        self.assertFalse(sc.verify_enc_key(first, self.salt, os.urandom(32)))

    def test_decrypt_roundtrip_and_header(self):
        pages = sc.decrypt_db(self.tmp, self.raw_key, self.out)
        self.assertEqual(pages, len(self.contents))
        with open(self.out, "rb") as fh:
            plain = fh.read()
        self.assertEqual(plain[:sc.SALT_SZ], sc.SQLITE_HEADER)
        cipher_end = sc.PAGE_SIZE - sc.RESERVE
        self.assertEqual(plain[sc.SALT_SZ:cipher_end], self.contents[0])
        page1 = plain[sc.PAGE_SIZE:2 * sc.PAGE_SIZE]
        self.assertEqual(page1[:cipher_end], self.contents[1])

    def test_decrypt_with_enc_key(self):
        enc, _ = sc.derive_keys(self.raw_key, self.salt)
        pages = sc.decrypt_db_with_enc_key(self.tmp, enc, self.out)
        self.assertEqual(pages, len(self.contents))
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read()[:sc.SALT_SZ], sc.SQLITE_HEADER)

    def test_wrong_key_rejected_no_output(self):
        with self.assertRaises(ValueError):
            sc.decrypt_db(self.tmp, os.urandom(sc.KEY_SZ), self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_hex_key_accepted(self):
        pages = sc.decrypt_db(self.tmp, self.raw_key.hex(), self.out)
        self.assertEqual(pages, len(self.contents))


if __name__ == "__main__":
    unittest.main()


class StreamEquivalenceTest(unittest.TestCase):
    """The streaming decryptor must be byte-identical to the buffered one."""

    def test_stream_matches_buffered(self):
        raw_key = os.urandom(sc.KEY_SZ)
        blob, _salt, _contents = build_encrypted(raw_key, n_pages=6)
        base = os.path.join(os.environ.get("TEMP", "."), f"wxstream_{os.getpid()}")
        src = base + ".db"
        with open(src, "wb") as fh:
            fh.write(blob)
        buffered, streamed = base + ".buf", base + ".str"
        try:
            n1 = sc.decrypt_db(src, raw_key, buffered)
            n2 = sc.decrypt_db_stream(src, raw_key, streamed)
            self.assertEqual(n1, n2)
            with open(buffered, "rb") as a, open(streamed, "rb") as b:
                self.assertEqual(a.read(), b.read())
        finally:
            for p in (src, buffered, streamed, streamed + ".part"):
                if os.path.exists(p):
                    os.remove(p)

    def test_stream_rejects_wrong_key(self):
        raw_key = os.urandom(sc.KEY_SZ)
        blob, _s, _c = build_encrypted(raw_key, n_pages=3)
        base = os.path.join(os.environ.get("TEMP", "."), f"wxstreambad_{os.getpid()}")
        src, dst = base + ".db", base + ".plain"
        with open(src, "wb") as fh:
            fh.write(blob)
        try:
            with self.assertRaises(ValueError):
                sc.decrypt_db_stream(src, os.urandom(sc.KEY_SZ), dst)
            self.assertFalse(os.path.exists(dst))
        finally:
            for p in (src, dst, dst + ".part"):
                if os.path.exists(p):
                    os.remove(p)
