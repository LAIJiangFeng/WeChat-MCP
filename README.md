# WeChat Backup MCP

让 AI 客户端（Claude Code / Codex / Cursor 等）**只读查询你本人设备上的微信 4.x 聊天记录**。通过 MCP 暴露五个受控工具：

| 工具 | 用途 |
| --- | --- |
| `list_conversations(limit)` | 按最后消息时间列出会话 |
| `list_contacts(query, limit)` | 列出或搜索联系人 |
| `read_chat_history(conversation, limit)` | 读取一个会话最近的消息 |
| `search_messages(keyword, limit)` | 跨会话搜索消息 |
| `analyze_message(message, relation, severity_hint)` | 判断一条消息的意图、情绪、诉求、严重程度，并给出回复策略 |

> ⚠️ **仅限你本人拥有或已获明确授权的账号与设备。** 本项目不内置微信程序，不在仓库存放任何密钥或聊天数据，解密过程不联网。请勿用于他人账号。

## 一键安装（Windows）

微信需正在运行并已登录。一条命令完成解密并生成客户端配置：

```powershell
uvx --from "wechat-backup-mcp[decrypt] @ git+https://github.com/LAIJiangFeng/WeChat-MCP" wechat-backup-setup
```

若你的 uv 版本不支持上面的写法，用等价的：

```powershell
uvx --from git+https://github.com/LAIJiangFeng/WeChat-MCP --with pycryptodome --with psutil --with pefile wechat-backup-setup
```

向导会：自动找到微信数据目录 → 从内存提取密钥（首次几分钟，之后缓存加速）→ 解密到默认备份目录 → 校验 → **打印**填好真实路径的 Claude Code / Codex / Cursor / VS Code 配置，你复制粘贴即可。向导只打印、不改动你的任何客户端配置文件。

装好后保持最新（微信有新消息时重新同步，通常几秒）：

```powershell
wechat-backup-refresh              # 同步一次
wechat-backup-refresh --watch 60   # 常驻，每 60 秒自动同步
```

**架构与安全边界**：MCP 服务端（`wechat_mcp`）是**跨平台、纯只读**的，本体不含任何解密/密钥/进程内存代码——这一点由 `tests/test_boundary.py` 强制保证。解密工具（`wechat_decrypt`）是独立的 Windows 专属命令，装在与服务端不同的环境里。设计参考 [NoCannoBB《微信记录 AI 助手》](https://nocannobb.com/course/wechat-ai-assistant/04-NoCannoBB-Skill)。

macOS / Linux 用户：解密只能在 Windows 上做，但你可以把 Windows 上生成的明文备份拷过来，然后用 `wechat-backup-setup --configure-only --backup-dir <目录>` 只生成配置。

## 消息情绪与回复策略（TypeSafe Jev）

`analyze_message(message, relation, severity_hint, context)` 优先调用 [TypeSafe](https://docs.typesafe.ai) 的 System One 模型 **Jev**：一次请求同时问 8 个类型化问题（意图、情绪、细分状态、诉求各一个 Choice，严重程度一个 Score，以及“是否明确要建议 / 是否哈哈带过 / 是否反话”三个 Noul），代码再把答案映射成回复策略。没有密钥或调用失败时自动退回离线关键词规则。

| 环境变量 | 说明 |
| --- | --- |
| `TYPESAFE_API_KEY` | TypeSafe 密钥，在 [console.typesafe.ai/keys](https://console.typesafe.ai/keys) 创建。只放在 MCP 配置的 `env` 里，不要提交到 Git |
| `TYPESAFE_DEFAULT_MODEL` | 可选，默认 `jev-latest` |
| `EMPATHY_BACKEND` | `auto`（默认：有密钥用 Jev，否则规则；Jev 报错回退规则）/ `jev`（强制，出错抛出）/ `rules`（永不联网） |

> Jev 主要训练语言是英语，中文准确率官方标注为较低。问题的判据写成了中英双语，返回的 `confidence` / `probabilities` 用来提示不确定项；请用自己的聊天样本验证阈值。

返回字段：

| 字段 | 取值 |
| --- | --- |
| `intent` | `vent` 表达感情 / `solve` 想要解决答案 / `chat` 纯聊天 |
| `emotion` | 快乐、悲伤、愤怒、恐惧、惊讶、厌恶（无明显线索时为 平静） |
| `state` | 开心 / 难过 / 生气 / 委屈 / 焦虑 / 失望 / 尴尬 / 疲惫 |
| `need` | 想倾诉 / 想安慰 / 想解决问题 / 想要道歉 / 想要空间 / 想被重视 |
| `severity` | 小事 / 认真对待 / 重大打击 |
| `relation` | 由 `relation` 参数归一化：朋友 / 恋人 / 同事 / 领导 / 客户 / 不熟 |
| `signals` | Jev：各项答案与置信度、三个 Noul 线索；规则：命中的词和标点 |
| `source` | `jev` 或 `rules`；回退时附 `jev_error` 说明原因 |
| `confidence` / `probabilities` / `uncertain` | 仅 Jev：每个问题的置信度、完整概率分布，以及置信度低于 0.4 的问题及其前两名选项 |
| `cues` | 仅 Jev：`wants_advice` / `is_deflecting` / `is_sarcastic` 三个 0–1 概率 |
| `reply_strategy` | 回复顺序（先理解情绪 → 再回应事情 → 最后给建议）、语气、要做 / 避免、时机不对时别说的话、可参考的开头 |

同时提供 MCP prompt `empathetic_reply(message, relation, context)`，把 Jev（或规则）判断和完整判断准则一起交给客户端模型去拟回复。
判断只是第一层参考；置信度低、`is_sarcastic` 高或群聊玩笑时，仍需模型结合上下文修正。

## 支持的数据

本项目读取微信 4.x 已解密的 SQLite 备份，兼容常见的平铺目录，以及 WeFlow 等工具使用的分目录结构：

```text
<backup>/
├── contact/contact.db
└── message/
    ├── message_0.db
    ├── message_1.db
    └── ...
```

也可将 `contact.db`、`message_0.db` 放在同一目录。配置目录中只能有**一个账号、一个备份快照**；发现多组数据库时会拒绝猜测。

> **MCP 服务端**不提取密钥、不解密、不连接运行中的微信，也不读 JSON/HTML 导出——它只读已解密的 SQLite。生成明文备份是**独立的** `wechat-backup-setup` / `wechat-backup-refresh` 命令的职责（仅 Windows），两者装在不同环境、互不引用。

## 手动配置（不想用向导时）

向导会替你打印好，但如果你想手填，`WECHAT_BACKUP_DIR` 要指向**单个账号子目录**（`<备份根>/<wxid>`），不是备份根目录——否则遇到多账号会报错。

**Claude Code**（`<服务端路径>` 用 `wechat-backup-mcp` 装好后的绝对路径，或 `uv tool install` 后的命令名）：

```powershell
claude mcp add wechat-backup --scope user `
  -e WECHAT_BACKUP_DIR="C:/.../backup/wxid_你的账号" `
  -e SANITIZE=1 `
  -- wechat-backup-mcp
```

**通用 JSON**（Cursor / Claude Desktop 的 `mcpServers`）：

```json
{
  "mcpServers": {
    "wechat-backup": {
      "command": "wechat-backup-mcp",
      "args": [],
      "env": { "WECHAT_BACKUP_DIR": "C:/.../backup/wxid_你的账号", "SANITIZE": "1" }
    }
  }
}
```

**Codex CLI**（`~/.codex/config.toml`，用 TOML 字面量字符串免转义）：

```toml
[mcp_servers.wechat-backup]
command = 'wechat-backup-mcp'
args = []
env = { WECHAT_BACKUP_DIR = 'C:/.../backup/wxid_你的账号', SANITIZE = '1' }
```

- `SANITIZE=1`：对返回结果里的 wxid / 群 ID 打码（昵称不受影响），推荐开启。
- `TYPESAFE_API_KEY`（可选）：加到 `env` 里可启用 Jev 情绪分析，不填则用离线规则；密钥来自 [console.typesafe.ai/keys](https://console.typesafe.ai/keys)，**不要写进仓库**。
- `WECHAT_PLAIN_DIR` 是 `WECHAT_BACKUP_DIR` 的兼容别名。

## Skill（可选）

`SKILL.md` 已随包分发（`wechat_mcp/skill/SKILL.md`）。把它复制到客户端的 skills 目录即可让模型自动编排这些工具，例如 Claude Code：

```text
.claude/skills/wechat-backup/SKILL.md
```

Skill 会完成这类编排：

```text
“总结我和张三最近聊了什么”
→ list_contacts(query="张三")
→ read_chat_history(conversation="Msg_...", limit=50)
→ 模型先总结结论和待办，再引用少量关键原文
```

## 安全边界

- 仅用于你本人拥有或明确获授权的账号与设备。
- 所有数据库连接都使用 SQLite `mode=ro`，并启用 `PRAGMA query_only=ON`。
- 动态消息表名只接受 `Msg_<32位十六进制>`；查询值全部参数化。
- MCP 不提供任意 SQL、发消息、改库、删库、密钥读取或整库导出工具。
- 不要把明文数据库、密钥或大批聊天原文提交到 Git、Issue 或外部服务。
- `search_messages` 为保持备份零修改会做只读全表扫描；备份很大时可能较慢。

## 常见问题

### `file is not a database` / “不是微信原始加密库”

配置指向了微信的 SQLCipher 加密原库。这个 MCP 只读明文备份，请先在本机完成合法备份/解密。

### “找到多组消息备份”

当前目录同时包含多个账号或多个时间点。把 `WECHAT_BACKUP_DIR` 缩小到单个账号、单个快照。

### 找得到联系人但没有消息

联系人可能没有聊天记录，或对应的 `message_N.db` 没有被备份。先调用 `list_conversations` 查看实际可读会话。

### 搜索较慢

压缩消息需要逐条 zstd 解压后匹配，且数据库以只读方式打开，项目不会偷偷创建索引。先缩小到具体会话使用 `read_chat_history`；只有实测需要时再考虑单独构建本地全文索引。

## 开发与测试

```powershell
git clone https://github.com/LAIJiangFeng/WeChat-MCP
cd WeChat-MCP
uv sync --extra decrypt     # decrypt extra 供解密工具和 sqlcipher 测试使用
uv run python -m unittest   # 49 项测试，全程离线
```

`tests/test_boundary.py` 保证服务端不引入任何解密/密钥能力；`tests/test_sqlcipher4.py` 用合成数据验证解密核心；均不需要真实微信。
