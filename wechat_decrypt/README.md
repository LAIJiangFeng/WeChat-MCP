# wechat_decrypt — 一次性解密工具（Windows）

把微信 4.x 的 **SQLCipher-4 加密库**解密成明文 SQLite，供只读的 `wechat_mcp` 使用。
本工具**刻意独立于 MCP**：只有它会读取微信进程内存、接触密钥；MCP 永远不碰密钥，
也不做解密（架构见 NoCannoBB 课程第 0 章）。

## 与 MCP 的关系

```
加密库 (D:\xwechat_files\<wxid>\db_storage\)
   │  ← 本工具跑一次：取密钥 + 逐页 AES 解密
   ▼
明文备份 (D:\wechat_backup\<wxid>\message\*.db, contact\contact.db)
   │  ← wechat_mcp 只读打开 (mode=ro)，WECHAT_BACKUP_DIR 指向它
   ▼
analyze_message / search_messages / read_chat_history / list_* 工具
```

## 依赖

`pycryptodome`（AES）、`psutil`（找主进程）、`pefile`（解析 DLL，可选）。已在 pyproject
的可选组 `decrypt` 里；安装：`uv pip install pycryptodome psutil pefile`。

## 用法

```powershell
# 先自证加密内核（不碰微信）

# 微信已登录运行时，一条命令解密全部账号到 D:\wechat_backup
uv run python -m wechat_decrypt.main_win

# 指定输出目录 / 单个账号 / 打印完整密钥
uv run python -m wechat_decrypt.main_win --out D:\wechat_backup --wxid wxid_xxx --dump-key

# 内存扫描失败时的回退
uv run python -m wechat_decrypt.main_win --key <64位hex>            # 直接给库密钥
uv run python -m wechat_decrypt.main_win --internal-key <64位hex>  # 只手动给 DLL 内部密钥
uv run python -m wechat_decrypt.main_win --pid 1234 --dll "D:\...\Weixin.dll"
```

跑完后，把 MCP 客户端配置里的 `WECHAT_BACKUP_DIR` 指向 `--out\<wxid>`（工具会打印确切路径）。

## 模块

| 文件 | 职责 |
| --- | --- |
| `sqlcipher4.py` | 密钥派生、HMAC 校验、逐页 AES 解密（纯 stdlib + pycryptodome）|
| `mem_windows.py` | OpenProcess / VirtualQueryEx / ReadProcessMemory（纯 ctypes）|
| `dll_scan_win.py` | 扫 Weixin.dll 取 internal_db_key（pefile 优先，回退整文件）|
| `key_extractor_win.py` | 内存里找原始密钥候选 + XOR 合并 + 首页 HMAC 精确验证 |
| `paths_win.py` | 数据根 / 账号 / 加密库路径发现（注册表 + 常见盘符）|
| `main_win.py` | CLI，把上面串起来 |

## 安全边界

- 仅用于**你自己、已授权**的设备与账号。
- 密钥只在本机内存中出现；默认脱敏打印，`--dump-key` 才显示完整值。
- 明文库、密钥、聊天记录**绝不提交 Git**（见根目录 `.gitignore`）。
- 本工具不连接运行中的微信数据库、不执行任意 SQL；解密是一次性离线操作。

## 故障排查

- **找不到密钥候选**：确认微信已登录；多开时用 `--pid` 指定主进程；必要时用管理员
  PowerShell 运行。
  或 `--key` 手动传。
- **DLL 无候选**：`--dll` 指定路径，或放宽 `dll_scan_win._PATTERN` 的间距。

## 保持备份最新（refresh_win）

`main_win` 是首次全量解密；日常增量刷新用 `refresh_win`，它只解密**源文件变化过的**库，
并把验证通过的密钥用 **Windows DPAPI** 缓存在 `%LOCALAPPDATA%\wechat-mcp\dbkeys.dpapi`，
免去每次几分钟的内存扫描。

```bash
# 刷新一次（约 3 秒；首次会扫一次内存取密钥）
python -m wechat_decrypt.refresh_win

# 常驻守护：每 30 秒自动同步，MCP 查到的永远是最新记录
python -m wechat_decrypt.refresh_win --watch 30

# 删除密钥缓存
python -m wechat_decrypt.refresh_win --forget-key
```

要点：

- **密钥是固定的**（就是库的 SQLCipher 密钥），缓存后复用；一旦失效会自动重新扫描。
- DPAPI 密文绑定当前 Windows 账户，拷到别的机器或别的用户都解不开；文件在仓库之外。
- 解密是**流式**的（`decrypt_db_stream`），内存占用恒定为一页，适合长期驻留。
- 先写 `*.new` 暂存并确认能作为 SQLite 打开，再原子替换，避免微信写入过程中产生半截文件。
- MCP 始终只读，不持有密钥、不接触运行中的微信——刷新由这个独立进程完成。
