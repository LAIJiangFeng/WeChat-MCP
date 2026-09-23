"""One-time WeChat 4.x SQLCipher-4 decryption CLI.

This package is deliberately SEPARATE from the read-only ``wechat_mcp`` server.
It reads WeChat process memory to recover the database key and decrypts the
encrypted SQLite files into a plaintext backup directory that the read-only MCP
then points at. The MCP itself never touches keys or process memory.

Follows the NoCannoBB course (wechat-ai-assistant, chapters 00-01); the crypto
parameters are the same ones used by pywxdump / chatlog / wechat-dump-rs.
"""

__all__ = ["sqlcipher4"]
