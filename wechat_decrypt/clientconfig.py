"""Render ready-to-paste MCP client configuration.

The wizard only ever prints these. Client config files are hand-maintained and
carry the user's other servers, so we do not rewrite them.

Kept portable (no Windows imports) and side-effect free so it can be unit tested.
"""
from __future__ import annotations

import json

SERVER_NAME = "wechat-backup"


def _posix(path: str) -> str:
    """Windows paths with forward slashes: valid everywhere, no JSON escaping traps."""
    return path.replace("\\", "/")


def env_block(backup_dir: str, sanitize: bool = True,
              typesafe_key: str | None = None) -> dict[str, str]:
    env = {"WECHAT_BACKUP_DIR": _posix(backup_dir), "SANITIZE": "1" if sanitize else "0"}
    if typesafe_key:
        env["TYPESAFE_API_KEY"] = typesafe_key
    return env


def claude_code_command(server_cmd: str, backup_dir: str, sanitize: bool = True) -> str:
    """A single `claude mcp add` line for Claude Code."""
    parts = [f"claude mcp add {SERVER_NAME} --scope user"]
    for key, value in env_block(backup_dir, sanitize).items():
        parts.append(f'-e {key}="{value}"')
    parts.append(f'-- "{_posix(server_cmd)}"')
    return " ".join(parts)


def generic_json(server_cmd: str, backup_dir: str, sanitize: bool = True) -> str:
    """The `mcpServers` shape used by Cursor, Claude Desktop and most clients."""
    return json.dumps(
        {
            "mcpServers": {
                SERVER_NAME: {
                    "command": _posix(server_cmd),
                    "args": [],
                    "env": env_block(backup_dir, sanitize),
                }
            }
        },
        indent=2,
        ensure_ascii=False,
    )


def codex_toml(server_cmd: str, backup_dir: str, sanitize: bool = True) -> str:
    """A `~/.codex/config.toml` block.

    Uses TOML literal strings so Windows backslashes need no escaping.
    """
    env = env_block(backup_dir, sanitize)
    pairs = ", ".join(f"{k} = '{v}'" for k, v in env.items())
    return (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"command = '{_posix(server_cmd)}'\n"
        f"args = []\n"
        f"env = {{ {pairs} }}\n"
    )


def vscode_json(server_cmd: str, backup_dir: str, sanitize: bool = True) -> str:
    """VS Code's `.vscode/mcp.json` uses a top-level `servers` key and a type."""
    return json.dumps(
        {
            "servers": {
                SERVER_NAME: {
                    "type": "stdio",
                    "command": _posix(server_cmd),
                    "args": [],
                    "env": env_block(backup_dir, sanitize),
                }
            }
        },
        indent=2,
        ensure_ascii=False,
    )


def render_all(server_cmd: str, backup_dir: str, sanitize: bool = True) -> str:
    """Everything a user might need to paste, labelled per client."""
    return "\n".join(
        [
            "--- Claude Code ---",
            claude_code_command(server_cmd, backup_dir, sanitize),
            "",
            "--- Codex CLI  (~/.codex/config.toml) ---",
            codex_toml(server_cmd, backup_dir, sanitize),
            "--- Cursor / Claude Desktop  (mcpServers) ---",
            generic_json(server_cmd, backup_dir, sanitize),
            "",
            "--- VS Code  (.vscode/mcp.json) ---",
            vscode_json(server_cmd, backup_dir, sanitize),
        ]
    )
