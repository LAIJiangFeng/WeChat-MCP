"""Discover the WeChat data root and the encrypted DB files to decrypt."""
from __future__ import annotations

import glob
import os
import re
import winreg
from dataclasses import dataclass, field


@dataclass
class AccountDbs:
    wxid: str
    root: str                       # ...\<wxid>\db_storage
    message_dbs: list[str] = field(default_factory=list)
    contact_db: str | None = None


def _existing_drives() -> list[str]:
    """Drive letters that actually exist, so a data root on any disk is found."""
    return [f"{chr(c)}:" for c in range(ord("A"), ord("Z") + 1)
            if os.path.isdir(f"{chr(c)}:" + os.sep)]


def _file_save_path_from_registry() -> str | None:
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Tencent\Weixin") as k:
                val, _ = winreg.QueryValueEx(k, "FileSavePath")
                if val:
                    return val
        except OSError:
            continue
    return None


def find_data_roots(explicit: str | None = None) -> list[str]:
    """Return candidate <data-root>/xwechat_files directories."""
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    reg = _file_save_path_from_registry()
    if reg:
        cands.append(reg if reg.lower().endswith("xwechat_files")
                     else os.path.join(reg, "xwechat_files"))
    for drive in _existing_drives():
        cands.append(drive + os.sep + "xwechat_files")
    cands.append(os.path.expanduser(r"~\Documents\xwechat_files"))
    out, seen = [], set()
    for c in cands:
        c = os.path.normpath(c)
        if c.lower() not in seen and os.path.isdir(c):
            seen.add(c.lower())
            out.append(c)
    return out


def discover_accounts(data_root: str) -> list[AccountDbs]:
    r"""Under an xwechat_files root, find each <wxid>\db_storage with real dbs."""
    accounts: list[AccountDbs] = []
    for entry in sorted(os.listdir(data_root)):
        db_storage = os.path.join(data_root, entry, "db_storage")
        if not os.path.isdir(db_storage):
            continue
        msg = sorted(glob.glob(os.path.join(db_storage, "message", "message_*.db")))
        # only per-conversation message_N.db, not message_fts.db / message_resource.db
        msg = [m for m in msg
               if re.match(r"message_\d+\.db$", os.path.basename(m), re.IGNORECASE)]
        contact = os.path.join(db_storage, "contact", "contact.db")
        contact = contact if os.path.isfile(contact) else None
        if msg or contact:
            accounts.append(AccountDbs(entry, db_storage, msg, contact))
    return accounts
