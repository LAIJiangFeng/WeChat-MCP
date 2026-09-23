"""Where things live, on any machine.

Kept free of Windows imports so the setup wizard can still render paths and
client config on macOS/Linux, where decryption itself is impossible.
"""
from __future__ import annotations

import os
import shutil
import sys

APP_NAME = "wechat-backup-mcp"

# A WeChat backup is routinely several GB. Ask for headroom over the encrypted
# size so a refresh that grows the database does not fill the disk.
SPACE_MARGIN = 1.2


def _app_data_dir() -> str:
    """Per-OS application data directory (not a hardcoded drive)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
    return os.path.join(base, APP_NAME)


def default_backup_dir() -> str:
    """Default plaintext backup root. ``WECHAT_BACKUP_DIR`` wins when set."""
    return os.environ.get("WECHAT_BACKUP_DIR") or os.path.join(_app_data_dir(), "backup")


def account_dir(backup_root: str, wxid: str) -> str:
    """The directory an MCP client should be pointed at.

    The server refuses to guess between accounts, so config always names one.
    """
    return os.path.join(backup_root, wxid)


def free_bytes(path: str) -> int:
    """Free space on the volume that would hold ``path`` (walks up if needed)."""
    probe = os.path.abspath(path)
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def has_room_for(path: str, needed_bytes: int) -> bool:
    return free_bytes(path) >= needed_bytes * SPACE_MARGIN


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024 or unit == "TB":
            return f"{num_bytes:.0f} {unit}" if unit in ("B", "KB") else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def roomier_alternative(preferred: str, needed_bytes: int, hint_path: str | None = None) -> str | None:
    """Suggest somewhere with space when ``preferred`` is too tight.

    Prefers the volume that already holds the source data, since it is
    demonstrably large enough to hold WeChat itself.
    """
    if has_room_for(preferred, needed_bytes):
        return None
    if hint_path:
        drive = os.path.splitdrive(os.path.abspath(hint_path))[0]
        if drive:
            candidate = os.path.join(drive + os.sep, APP_NAME, "backup")
            if has_room_for(candidate, needed_bytes):
                return candidate
    home = os.path.join(os.path.expanduser("~"), "wechat_backup")
    return home if has_room_for(home, needed_bytes) else None
