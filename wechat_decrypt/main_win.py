"""One-command WeChat 4.x decrypt CLI (Windows).

    找主进程 -> 定位加密库 -> 取密钥 -> 逐页解密 -> 落成明文备份

Output layout mirrors the source so the read-only MCP finds it via rglob:

    <out>/<wxid>/message/message_0.db
    <out>/<wxid>/contact/contact.db

The MCP expects a single account per WECHAT_BACKUP_DIR, so point it at
<out>/<wxid> (the tool prints the exact path to set).

Examples:
    uv run python -m wechat_decrypt.main_win                 # -> ~/wechat_backup
    uv run python -m wechat_decrypt.main_win --out /path/to/backup
    uv run python -m wechat_decrypt.main_win --dump-key
    uv run python -m wechat_decrypt.main_win --key <64hex>   # skip memory scan
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

from wechat_decrypt import dll_scan_win, key_extractor_win, mem_windows, paths_win
from wechat_decrypt import sqlcipher4 as sc


def _mask(hex_key: str) -> str:
    return hex_key[:6] + "…" + hex_key[-6:] if len(hex_key) > 12 else "…"


def _verify_plaintext(path: str) -> str:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tbls = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            msg = [t for t in tbls if t.lower().startswith("msg_")]
            if msg:
                n = sum(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                        for t in msg[:200])
                return f"OK: {len(msg)} 会话表, 抽样 {n} 条消息"
            return f"OK: {len(tbls)} 张表 ({', '.join(tbls[:5])})"
        finally:
            con.close()
    except Exception as e:
        return f"打开失败: {e}"


def _decrypt_one(src: str, found: key_extractor_win.FoundKey, dst: str) -> int:
    """Decrypt one database and return the page count (used in the progress line)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if found.method == "enc":
        return sc.decrypt_db_with_enc_key(src, found.hex_key, dst)
    # Stream: a full account can be hundreds of MB and the buffered path needs
    # two copies of the file in memory.
    return sc.decrypt_db_stream(src, found.hex_key, dst)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WeChat 4.x one-time decrypt (Windows)")
    ap.add_argument("--out", default=refresh_win.default_backup_dir(),
                    help="输出明文备份根目录 (默认 ~/wechat_backup)")
    ap.add_argument("--data-root", help="xwechat_files 根目录 (默认自动发现)")
    ap.add_argument("--wxid", help="只解密某个账号 (默认全部发现的账号)")
    ap.add_argument("--pid", type=int, help="手动指定微信主进程 pid")
    ap.add_argument("--dll", help="手动指定 Weixin.dll 路径")
    ap.add_argument("--internal-key", help="手动传入 64 位 hex 的 internal_db_key")
    ap.add_argument("--key", help="直接提供 64 位 hex 数据库密钥, 跳过内存扫描")
    ap.add_argument("--dump-key", action="store_true", help="打印完整密钥 (默认脱敏)")
    args = ap.parse_args(argv)

    t0 = time.time()

    # locate encrypted dbs
    if args.data_root:
        roots = [args.data_root]
    else:
        roots = paths_win.find_data_roots()
    accounts = []
    for r in roots:
        for acc in paths_win.discover_accounts(r):
            if not args.wxid or acc.wxid == args.wxid:
                accounts.append(acc)
    if not accounts:
        print(f"未发现任何账号的加密库 (searched roots: {roots})", file=sys.stderr)
        return 2
    print(f"发现 {len(accounts)} 个账号:")
    for acc in accounts:
        print(f"  {acc.wxid}: {len(acc.message_dbs)} 个消息库"
              f"{' + contact.db' if acc.contact_db else ''}")

    # gather all target dbs (for key discovery we only need one first-page each)
    all_dbs: list[str] = []
    for acc in accounts:
        all_dbs += acc.message_dbs
        if acc.contact_db:
            all_dbs.append(acc.contact_db)

    # --- key discovery ---
    manual_key = args.key.strip() if args.key else None
    internal_keys: list[bytes] = []
    candidates: list[bytes] = []
    handle = None
    if not manual_key:
        if args.internal_key:
            internal_keys = [bytes.fromhex(args.internal_key.strip())]
        else:
            dll = dll_scan_win.resolve_weixin_dll(args.dll)
            internal_keys = dll_scan_win.extract_internal_keys(dll)
            print(f"Weixin.dll: {dll}\n  internal_db_key 候选: {len(internal_keys)} 个")
        pid = args.pid or mem_windows.find_weixin_pid()
        print(f"微信主进程 pid={pid}，扫描内存中…")
        handle = mem_windows.open_process(pid)
        try:
            candidates = key_extractor_win.scan_raw_key_candidates(handle)
        finally:
            mem_windows.close_process(handle)
        print(f"  原始密钥候选: {len(candidates)} 个 (用首页 HMAC 逐一验证)")
        if not candidates:
            print("内存里没找到密钥候选：确认微信已登录；必要时用管理员 PowerShell；"
                  "或用 --key 直接传密钥。", file=sys.stderr)
            return 3

    # --- per-account decrypt ---
    key_cache: dict[str, key_extractor_win.FoundKey] = {}
    total_ok = 0
    for acc in accounts:
        out_acc = os.path.join(args.out, acc.wxid)
        acc_dbs = list(acc.message_dbs) + ([acc.contact_db] if acc.contact_db else [])
        for src in acc_dbs:
            rel = os.path.relpath(src, acc.root)          # message/message_0.db …
            dst = os.path.join(out_acc, rel)
            # find a key for this db
            if manual_key:
                found = key_extractor_win.FoundKey(manual_key, "raw")
            else:
                found = None
                # reuse a previously verified key first (accounts often share one)
                for fk in key_cache.values():
                    first = sc.read_first_page(src); salt = first[:sc.SALT_SZ]
                    if fk.method == "enc":
                        ok = sc.verify_enc_key(first, salt, bytes.fromhex(fk.hex_key))
                    else:
                        ok = sc.verify_key(first, salt, bytes.fromhex(fk.hex_key))
                    if ok:
                        found = fk; break
                if found is None:
                    found = key_extractor_win.find_key_for_db(
                        handle if handle else 0, candidates, internal_keys, src)
                    # handle already closed; find_key_for_db only reads first page + verifies
                if found is None:
                    print(f"  [跳过] 未找到匹配密钥: {src}", file=sys.stderr)
                    continue
                key_cache[found.hex_key] = found
            try:
                pages = _decrypt_one(src, found, dst)
            except Exception as e:
                print(f"  [失败] {src}: {e}", file=sys.stderr)
                continue
            status = _verify_plaintext(dst)
            shown = found.hex_key if args.dump_key else _mask(found.hex_key)
            print(f"  [{found.method:>3}] {rel}  ({pages} 页)  key={shown}  {status}")
            total_ok += 1

        env_hint = out_acc
        print(f"→ 账号 {acc.wxid} 明文库已写入: {out_acc}")
        print(f"   MCP 用法: 把 WECHAT_BACKUP_DIR 指向  {env_hint}")

    dt = time.time() - t0
    print(f"\n完成: {total_ok} 个库解密成功，用时 {dt:.1f}s")
    return 0 if total_ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
