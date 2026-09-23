"""`wechat-backup-setup` — get a stranger from "installed" to "working".

Decryption only works on Windows with WeChat running and logged in. Everything
after it — picking an account, validating the result, printing client config —
works anywhere, so someone who copied a backup over from Windows can still use
this to configure their client.

Windows-only modules are imported inside functions on purpose: importing this
module must succeed on macOS and Linux.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

from wechat_decrypt import clientconfig, locations

IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- io

def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _step(n: int, total: int, title: str) -> None:
    _say(f"\n[{n}/{total}] {title}")


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = _ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer.startswith("y")


def _fail(msg: str, code: int = 1) -> int:
    print(f"\n✗ {msg}", file=sys.stderr, flush=True)
    return code


# ------------------------------------------------------------------- discovery

def _require_windows_deps() -> str | None:
    """Return a human explanation when decryption can't run here."""
    if not IS_WINDOWS:
        return (
            "解密只能在装有微信的 Windows 上进行（密钥只存在于运行中的微信进程内存里）。\n"
            "  如果你已经在 Windows 上做好了明文备份并拷贝了过来，用：\n"
            "      wechat-backup-setup --configure-only --backup-dir <备份目录>"
        )
    missing = [m for m in ("Crypto", "psutil", "pefile") if not _importable(m)]
    if missing:
        names = {"Crypto": "pycryptodome", "psutil": "psutil", "pefile": "pefile"}
        pkgs = " ".join(names[m] for m in missing)
        return f"缺少解密依赖。请安装后重试：  uv pip install {pkgs}"
    return None


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _encrypted_size(account) -> int:
    paths = list(account.message_dbs) + ([account.contact_db] if account.contact_db else [])
    return sum(os.path.getsize(p) for p in paths if os.path.exists(p))


def _choose_account(accounts, preset: str | None):
    if preset:
        for acc in accounts:
            if acc.wxid == preset:
                return acc
        raise SystemExit(f"找不到账号 {preset}；可用：{', '.join(a.wxid for a in accounts)}")
    if len(accounts) == 1:
        return accounts[0]

    _say("发现多个账号：")
    for i, acc in enumerate(accounts, 1):
        size = locations.human_size(_encrypted_size(acc))
        _say(f"  {i}. {acc.wxid}   {len(acc.message_dbs)} 个消息库, {size}")
    _say("\n只能配置一个账号（MCP 不会在多个账号之间猜测）。")
    while True:
        choice = _ask(f"选择账号 [1-{len(accounts)}]", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(accounts):
            return accounts[int(choice) - 1]
        _say("请输入列表中的编号。")


# ----------------------------------------------------------------- server path

def _server_command() -> str:
    """Absolute path to the installed MCP server, so clients need no PATH setup."""
    found = shutil.which("wechat-backup-mcp")
    if found:
        return found
    # Running from a source checkout: point at this interpreter's script dir.
    guess = os.path.join(os.path.dirname(sys.executable), "wechat-backup-mcp.exe"
                         if IS_WINDOWS else "wechat-backup-mcp")
    return guess if os.path.exists(guess) else "wechat-backup-mcp"


# ---------------------------------------------------------------------- phases

def _decrypt_phase(args, total: int) -> tuple[str, str] | None:
    """Windows-only. Returns (backup_root, wxid) or None when it could not run."""
    from wechat_decrypt import paths_win, refresh_win

    _step(2, total, "查找微信数据目录")
    roots = paths_win.find_data_roots(args.data_root)
    if not roots:
        _say("未自动找到微信数据目录（xwechat_files）。")
        manual = _ask("请手动输入路径（留空放弃）")
        if not manual or not os.path.isdir(manual):
            raise SystemExit(_fail("没有可用的微信数据目录。微信 设置→文件管理 可以看到实际位置。", 2))
        roots = [manual]
    _say(f"  数据目录：{roots[0]}")

    accounts = [a for r in roots for a in paths_win.discover_accounts(r)]
    if not accounts:
        raise SystemExit(_fail("该目录下没有发现任何账号的数据库。", 2))
    account = _choose_account(accounts, args.wxid)
    need = _encrypted_size(account)
    _say(f"  选定账号：{account.wxid}（加密数据 {locations.human_size(need)}）")

    _step(3, total, "选择备份输出目录")
    out_root = args.out or locations.default_backup_dir()
    better = locations.roomier_alternative(out_root, need, hint_path=account.root)
    if better:
        _say(f"  ! {out_root} 剩余空间不足（需要约 {locations.human_size(need * 1.2)}）")
        _say(f"  建议改用：{better}")
        if not args.yes and _confirm("使用建议的目录？"):
            out_root = better
        elif args.yes:
            out_root = better
    if not args.yes:
        out_root = _ask("备份目录", out_root)
    _say(f"  输出到：{out_root}")

    _step(4, total, "提取密钥并解密（首次需要几分钟扫描内存）")
    _say("  需要微信正在运行且已登录。")
    try:
        updated = refresh_win.refresh_once(out_root, data_root=args.data_root,
                                           wxid=account.wxid, verbose=True)
    except Exception as exc:                                   # noqa: BLE001
        raise SystemExit(_fail(f"解密失败：{exc}", 3)) from exc
    if not updated and not os.path.isdir(locations.account_dir(out_root, account.wxid)):
        raise SystemExit(_fail(
            "没能取得密钥。请确认：微信已启动并完成登录；必要时用管理员身份运行本命令。", 3))
    return out_root, account.wxid


def _validate(backup_dir: str) -> bool:
    """Open the backup exactly like the MCP server will."""
    try:
        from wechat_mcp.server import WeChatBackup
    except Exception as exc:                                   # noqa: BLE001
        _say(f"  ! 无法载入 MCP 服务端做校验：{exc}")
        return False
    try:
        backup = WeChatBackup(backup_dir)
        convs = backup.list_conversations(limit=1)
        n_contacts = len(backup.list_contacts(limit=200))
    except Exception as exc:                                   # noqa: BLE001
        _say(f"  ✗ 备份无法读取：{exc}")
        return False
    if convs:
        _say(f"  ✓ 可读：最近会话「{convs[0]['name']}」，最后消息 {convs[0]['last_message_time']}")
    if n_contacts:
        _say(f"  ✓ 联系人索引正常（抽样到 {n_contacts} 条）")
    return True


# ------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="wechat-backup-setup",
        description="解密本机微信数据并生成 MCP 客户端配置（仅限你本人授权的账号与设备）",
    )
    ap.add_argument("--out", help="明文备份输出根目录")
    ap.add_argument("--data-root", help="微信 xwechat_files 目录（默认自动发现）")
    ap.add_argument("--wxid", help="直接指定账号，跳过选择")
    ap.add_argument("--configure-only", action="store_true",
                    help="跳过解密，只对已有备份生成配置")
    ap.add_argument("--backup-dir", help="配合 --configure-only：已有备份的账号目录")
    ap.add_argument("--no-sanitize", action="store_true",
                    help="配置中关闭脱敏（默认开启，会给 wxid 打码）")
    ap.add_argument("--yes", action="store_true", help="全部采用默认值，不交互")
    args = ap.parse_args(argv)

    total = 5
    _say("=" * 62)
    _say("  WeChat Backup MCP — 安装向导")
    _say("  仅用于你本人拥有或已获授权的账号与设备")
    _say("=" * 62)

    _step(1, total, "检查运行环境")
    blocker = _require_windows_deps()
    configure_only = args.configure_only or bool(args.backup_dir)

    if configure_only:
        _say("  配置模式：跳过解密")
        backup_dir = args.backup_dir or _ask("已有备份的账号目录（应包含 message/ 与 contact/）")
        if not backup_dir or not os.path.isdir(backup_dir):
            return _fail("该目录不存在。", 2)
    else:
        if blocker:
            _say(f"  ! {blocker}")
            return 3
        _say(f"  ✓ Windows，解密依赖齐备（Python {sys.version.split()[0]}）")
        result = _decrypt_phase(args, total)
        if result is None:
            return 3
        out_root, wxid = result
        backup_dir = locations.account_dir(out_root, wxid)

    _step(5 if configure_only else 5, total, "校验备份")
    if not _validate(backup_dir):
        return _fail("备份校验未通过，请检查上面的错误。", 4)

    server_cmd = _server_command()
    _say("\n" + "=" * 62)
    _say("  完成！把下面对应你客户端的配置复制过去即可")
    _say("=" * 62 + "\n")
    _say(clientconfig.render_all(server_cmd, backup_dir, sanitize=not args.no_sanitize))
    _say("\n保持备份最新（微信有新消息时重新同步，通常几秒）：")
    _say("    wechat-backup-refresh")
    _say("可选，常驻自动同步：")
    _say("    wechat-backup-refresh --watch 60")
    _say("\n可选，情绪/意图分析需要 TypeSafe 密钥（不填则用离线规则）：")
    _say("    在上面的 env 里加 TYPESAFE_API_KEY，密钥来自 https://console.typesafe.ai/keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
