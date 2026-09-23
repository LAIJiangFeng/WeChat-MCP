"""The MCP server must never be able to decrypt anything.

`wechat_mcp` is what an AI client talks to. It reads an already-decrypted
backup and nothing else: no key extraction, no process memory, no crypto. That
rule is easy to state in a README and easy to break in a refactor, so it is
asserted here instead.
"""
import ast
import os
import unittest

_SERVER_PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "wechat_mcp")

# Importing any of these from the server package would either pull in the
# decryption code or hand it the primitives to re-implement it.
FORBIDDEN = {
    "wechat_decrypt",   # the decrypt tool itself
    "Crypto",           # pycryptodome -> AES
    "psutil",           # locating the running WeChat process
    "pefile",           # parsing Weixin.dll
    "winreg",           # finding WeChat's install / data paths
    "ctypes",           # ReadProcessMemory, DPAPI
}


def _server_modules():
    for dirpath, _dirs, files in os.walk(_SERVER_PKG):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


class ServerCannotDecryptTest(unittest.TestCase):
    def test_no_forbidden_imports(self) -> None:
        offences = []
        for path in _server_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name.split(".")[0] in FORBIDDEN:
                        offences.append(f"{os.path.basename(path)}:{node.lineno} -> {name}")
        self.assertEqual(
            offences, [],
            "wechat_mcp 必须保持只读，不得引入解密/密钥/进程内存能力：\n  "
            + "\n  ".join(offences),
        )

    def test_no_subprocess_escape_hatch(self) -> None:
        """No shelling out either — that would route around the import ban."""
        offences = []
        for path in _server_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name.split(".")[0] in {"subprocess", "multiprocessing"}:
                        offences.append(f"{os.path.basename(path)}:{node.lineno} -> {name}")
        self.assertEqual(offences, [], "server 不应启动子进程：\n  " + "\n  ".join(offences))

    def test_server_package_is_importable_without_decrypt_extra(self) -> None:
        """The server must start on a machine that never installed the crypto deps."""
        with open(os.path.join(_SERVER_PKG, "server.py"), encoding="utf-8") as fh:
            source = fh.read()
        for banned in FORBIDDEN:
            self.assertNotIn(f"import {banned}", source)


if __name__ == "__main__":
    unittest.main()
