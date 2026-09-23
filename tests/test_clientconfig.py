"""Rendering tests for the config the wizard prints.

These run on every platform: the point is that a Windows path survives the trip
into JSON and TOML without escaping bugs, which is exactly what users paste.
"""
import json
import os
import sys
import unittest
from unittest import mock

from wechat_decrypt import clientconfig, locations

WIN_DIR = r"C:\Users\me\AppData\Local\wechat-backup-mcp\backup\wxid_abc123"
CMD = r"C:\Users\me\.local\bin\wechat-backup-mcp.exe"


class ClientConfigTest(unittest.TestCase):
    def test_json_uses_forward_slashes_and_parses(self) -> None:
        parsed = json.loads(clientconfig.generic_json(CMD, WIN_DIR))
        server = parsed["mcpServers"]["wechat-backup"]
        self.assertNotIn("\\", server["command"])
        self.assertNotIn("\\", server["env"]["WECHAT_BACKUP_DIR"])
        self.assertTrue(server["env"]["WECHAT_BACKUP_DIR"].endswith("wxid_abc123"))

    def test_vscode_shape_differs_from_generic(self) -> None:
        parsed = json.loads(clientconfig.vscode_json(CMD, WIN_DIR))
        self.assertIn("servers", parsed)
        self.assertNotIn("mcpServers", parsed)
        self.assertEqual(parsed["servers"]["wechat-backup"]["type"], "stdio")

    def test_codex_toml_parses_and_never_doubles_backslashes(self) -> None:
        block = clientconfig.codex_toml(CMD, WIN_DIR)
        self.assertIn("[mcp_servers.wechat-backup]", block)
        # Literal strings ('...') mean a backslash is never an escape, so a
        # pasted Windows path can never turn into "\\U" or a mangled "\n".
        self.assertNotIn("\\\\", block)
        try:
            import tomllib
        except ModuleNotFoundError:            # pragma: no cover - py3.10
            return
        parsed = tomllib.loads(block)
        env = parsed["mcp_servers"]["wechat-backup"]["env"]
        # Paths are normalised to forward slashes, which Windows accepts too.
        self.assertEqual(env["WECHAT_BACKUP_DIR"], WIN_DIR.replace("\\", "/"))
        self.assertTrue(env["WECHAT_BACKUP_DIR"].endswith("wxid_abc123"))

    def test_sanitize_default_is_on(self) -> None:
        self.assertEqual(clientconfig.env_block(WIN_DIR)["SANITIZE"], "1")
        self.assertEqual(clientconfig.env_block(WIN_DIR, sanitize=False)["SANITIZE"], "0")

    def test_api_key_is_never_included_unless_asked(self) -> None:
        self.assertNotIn("TYPESAFE_API_KEY", clientconfig.env_block(WIN_DIR))
        self.assertIn("TYPESAFE_API_KEY", clientconfig.env_block(WIN_DIR, typesafe_key="apikey_x"))
        # and it must not leak into the rendered blocks by default
        self.assertNotIn("TYPESAFE_API_KEY", clientconfig.render_all(CMD, WIN_DIR))

    def test_claude_command_names_a_single_account_dir(self) -> None:
        cmd = clientconfig.claude_code_command(CMD, WIN_DIR)
        self.assertIn("claude mcp add wechat-backup", cmd)
        self.assertIn("wxid_abc123", cmd)


class LocationsTest(unittest.TestCase):
    def test_env_var_overrides_default(self) -> None:
        with mock.patch.dict(os.environ, {"WECHAT_BACKUP_DIR": "/tmp/custom"}):
            self.assertEqual(locations.default_backup_dir(), "/tmp/custom")

    def test_default_has_no_hardcoded_drive(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "WECHAT_BACKUP_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            path = locations.default_backup_dir()
        self.assertIn(locations.APP_NAME, path)
        if sys.platform != "win32":
            self.assertNotIn(":", path)

    def test_account_dir_is_per_account(self) -> None:
        self.assertTrue(
            locations.account_dir("/backup", "wxid_abc").endswith(
                os.path.join("wxid_abc")
            )
        )

    def test_space_check_rejects_absurd_request(self) -> None:
        self.assertFalse(locations.has_room_for(os.path.expanduser("~"), 10**18))
        self.assertTrue(locations.has_room_for(os.path.expanduser("~"), 1))

    def test_human_size(self) -> None:
        self.assertEqual(locations.human_size(512), "512 B")
        self.assertEqual(locations.human_size(283 * 1024 * 1024), "283.0 MB")


if __name__ == "__main__":
    unittest.main()
