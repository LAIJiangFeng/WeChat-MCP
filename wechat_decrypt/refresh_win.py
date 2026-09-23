"""Keep the plaintext backup in sync with WeChat's encrypted databases.

Only databases whose source file changed are decrypted, and the verified key is
cached with DPAPI, so a refresh costs a few seconds instead of a memory scan.

    python -m wechat_decrypt.refresh_win              # refresh once
    python -m wechat_decrypt.refresh_win --watch 30   # daemon, every 30s
    python -m wechat_decrypt.refresh_win --forget-key # drop cached keys

The read-only MCP never runs this: it only reads whatever is already on disk.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

from wechat_decrypt import (
    dll_scan_win,
    key_extractor_win,
    keystore_win,
    mem_windows,
    paths_win,
)
from wechat_decrypt import sqlcipher4 as sc

STATE_PATH = os.path.join(keystore_win.STORE_DIR, "refresh_state.json")
def default_backup_dir() -> str:
    """Where plaintext backups go unless the user says otherwise.

    Home-relative so it works on any machine and any drive layout.
    """
    return os.environ.get("WECHAT_BACKUP_DIR") or os.path.join(
        os.path.expanduser("~"), "wechat_backup"
    )

# Scanning WeChat's memory for a key costs minutes of CPU.  An account that is
# not currently logged in will never yield one, so back off instead of retrying
# on every tick.  {wxid: (earliest_retry_epoch, backoff_seconds)}
BACKOFF_PATH = os.path.join(keystore_win.STORE_DIR, "key_backoff.json")
_BACKOFF_START = 30 * 60
_BACKOFF_MAX = 6 * 60 * 60


def _mask(hex_key: str) -> str:
    return hex_key[:6] + "…" + hex_key[-4:]


def _load_state() -> dict[str, list]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict[str, list]) -> None:
    os.makedirs(keystore_win.STORE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


class _RotatingLog:
    """Size-capped log file: keeps the current file plus one previous one.

    The daemon appends a line per refresh and never exits on its own, so an
    unbounded log would grow on the system drive forever.
    """

    def __init__(self, path: str, max_bytes: int = 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._open()

    def _open(self) -> None:
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        self._size = self._fh.tell()

    def _rotate(self) -> None:
        self._fh.close()
        os.replace(self.path, self.path + ".1")   # previous log, overwritten
        self._open()

    def write(self, text: str) -> int:
        if self._size >= self.max_bytes:
            self._rotate()
        written = self._fh.write(text)
        self._size += len(text.encode("utf-8", "replace"))
        return written

    def flush(self) -> None:
        self._fh.flush()

    def isatty(self) -> bool:
        return False


def _load_backoff() -> dict[str, list]:
    try:
        with open(BACKOFF_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def _save_backoff(table: dict[str, list]) -> None:
    os.makedirs(keystore_win.STORE_DIR, exist_ok=True)
    tmp = BACKOFF_PATH + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(table, fh)
    os.replace(tmp, BACKOFF_PATH)


def _stamp(path: str) -> list:
    st = os.stat(path)
    return [int(st.st_mtime), st.st_size]


def _drop_sidecars(db_path: str) -> None:
    for suffix in ("-shm", "-wal"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass


def _plaintext_ok(db_path: str) -> bool:
    """Cheap sanity check that the file really is a usable SQLite database."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False
    finally:
        _drop_sidecars(db_path)


def _key_works(hex_key: str, method: str, src: str) -> bool:
    try:
        first = sc.read_first_page(src)
    except OSError:
        return False
    salt = first[:sc.SALT_SZ]
    raw = bytes.fromhex(hex_key)
    if method == "enc":
        return sc.verify_enc_key(first, salt, raw)
    return sc.verify_key(first, salt, raw)


def _discover_key(src: str) -> key_extractor_win.FoundKey | None:
    """Scan the running WeChat process for a key that opens ``src``."""
    dll = dll_scan_win.resolve_weixin_dll(None)
    internal_keys = dll_scan_win.extract_internal_keys(dll)
    pid = mem_windows.find_weixin_pid()
    if not pid:
        return None
    handle = mem_windows.open_process(pid)
    try:
        candidates = key_extractor_win.scan_raw_key_candidates(handle)
        return key_extractor_win.find_key_for_db(handle, candidates, internal_keys, src)
    finally:
        mem_windows.close_process(handle)


def _full_rewrite(src: str, hex_key: str, dst: str) -> int:
    """Decrypt into a staging file and only swap it in once it opens cleanly."""
    staging = dst + ".new"
    pages = sc.decrypt_db_stream(src, hex_key, staging)
    if not _plaintext_ok(staging):
        os.remove(staging)
        raise ValueError("decrypted file did not open as SQLite (torn read?)")
    os.replace(staging, dst)
    _drop_sidecars(dst)
    return pages


def _decrypt_verified(src: str, hex_key: str, dst: str) -> tuple[int, int]:
    """Update ``dst`` from ``src``, writing as few pages as possible.

    WeChat only appends, so refreshing in place costs a few kilobytes instead of
    rewriting tens of megabytes every time the daemon wakes up.  If the in-place
    result does not open as SQLite, fall back to a clean staged rewrite.
    """
    if not os.path.exists(dst):
        return _full_rewrite(src, hex_key, dst), -1
    try:
        pages, written = sc.decrypt_db_diff(src, hex_key, dst)
        if _plaintext_ok(dst):
            return pages, written
    except Exception:
        pass                                   # fall through to the safe path
    return _full_rewrite(src, hex_key, dst), -1


def refresh_once(out_root: str, *, data_root: str | None = None,
                 wxid: str | None = None, verbose: bool = True) -> int:
    """Decrypt every source database that changed since the last refresh."""
    roots = [data_root] if data_root else paths_win.find_data_roots()
    accounts = [acc for r in roots for acc in paths_win.discover_accounts(r)
                if not wxid or acc.wxid == wxid]
    if not accounts:
        print("未发现任何账号的加密库", file=sys.stderr)
        return 0

    keys = keystore_win.load()
    state = _load_state()
    backoff_tbl = _load_backoff()
    updated = 0
    keys_changed = False

    for acc in accounts:
        sources = list(acc.message_dbs) + ([acc.contact_db] if acc.contact_db else [])
        stale = [s for s in sources
                 if state.get(os.path.normcase(s)) != _stamp(s)
                 or not os.path.exists(os.path.join(out_root, acc.wxid,
                                                    os.path.relpath(s, acc.root)))]
        if not stale:
            continue

        entry = keys.get(acc.wxid)
        if entry and not _key_works(entry["key"], entry["method"], stale[0]):
            entry = None                      # cached key went stale
        if entry is None:
            until, backoff = backoff_tbl.get(acc.wxid, [0.0, _BACKOFF_START])
            if time.time() < until:
                continue                      # still cooling down, stay quiet
            if verbose:
                print(f"[{acc.wxid}] 缓存无可用密钥，扫描微信内存…", flush=True)
            found = _discover_key(stale[0])
            if found is None:
                backoff_tbl[acc.wxid] = [time.time() + backoff,
                                         min(backoff * 2, _BACKOFF_MAX)]
                _save_backoff(backoff_tbl)
                print(f"[{acc.wxid}] 未找到密钥（该账号可能未登录），"
                      f"{backoff/60:.0f} 分钟后重试", file=sys.stderr, flush=True)
                continue
            backoff_tbl.pop(acc.wxid, None)
            _save_backoff(backoff_tbl)
            entry = {"key": found.hex_key, "method": found.method}
            keys[acc.wxid] = entry
            keys_changed = True
            if verbose:
                print(f"[{acc.wxid}] 密钥已获取 ({found.method}) key={_mask(found.hex_key)}",
                      flush=True)

        for src in stale:
            rel = os.path.relpath(src, acc.root)
            dst = os.path.join(out_root, acc.wxid, rel)
            began = time.time()
            try:
                pages, written = _decrypt_verified(src, entry["key"], dst)
            except Exception as exc:                       # torn read, bad key…
                print(f"[{acc.wxid}] {rel} 刷新失败: {exc}", file=sys.stderr)
                continue
            state[os.path.normcase(src)] = _stamp(src)
            updated += 1
            if verbose:
                how = "全量" if written < 0 else f"回写 {written} 页"
                print(f"[{acc.wxid}] {rel} 已更新 ({pages} 页, {how}, "
                      f"{time.time()-began:.1f}s)", flush=True)

    if keys_changed:
        keystore_win.save(keys)
    if updated:
        _save_state(state)
    return updated


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="增量刷新微信明文备份")
    ap.add_argument("--out", default=default_backup_dir(), help="明文备份根目录")
    ap.add_argument("--data-root", help="xwechat_files 根目录（默认自动发现）")
    ap.add_argument("--wxid", help="只刷新某个账号")
    ap.add_argument("--watch", type=int, metavar="SEC",
                    help="常驻守护：每 SEC 秒检查一次源库是否变化")
    ap.add_argument("--forget-key", action="store_true", help="删除 DPAPI 密钥缓存后退出")
    ap.add_argument("--log", metavar="FILE",
                    help="把输出追加写入文件（pythonw 无控制台时使用）")
    args = ap.parse_args(argv)

    if args.log:
        sys.stdout = sys.stderr = _RotatingLog(args.log)
        print("")
        print(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} 守护启动 =====", flush=True)

    if args.forget_key:
        print("已删除密钥缓存" if keystore_win.forget() else "没有密钥缓存")
        return 0

    if not args.watch:
        n = refresh_once(args.out, data_root=args.data_root, wxid=args.wxid)
        print(f"刷新完成：{n} 个库更新" if n else "已是最新，无需刷新")
        return 0

    print(f"守护已启动：每 {args.watch}s 检查一次 → {args.out}（Ctrl+C 停止）", flush=True)
    try:
        while True:
            try:
                refresh_once(args.out, data_root=args.data_root, wxid=args.wxid)
            except Exception as exc:                        # keep the daemon alive
                print(f"本轮刷新异常: {exc}", file=sys.stderr, flush=True)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\n守护已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
