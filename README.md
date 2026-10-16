# Agent Session Bridge

终端会话迁移工具 + AI skill。单个会话、会话 ID、项目目录批量转换；复制或剪切。

支持 **Codex、Pi、DeepSeek Harness (DSH)、OpenCode、Qwen Code、Claude Code、WorkBuddy、Antigravity CLI (agy)** 互转。
本项目使用 MIT 协议开源；Claude Code、WorkBuddy、Antigravity 是兼容目标，不代表这些客户端开源。

复用 MIT 项目 `session-migrate==0.11.0` 的格式适配器，补充 DSH v4 和 Windows 写入。
所有处理在本机进行，不调用模型，不上传会话。
只迁移用户/助手对话、压缩摘要，以及目标支持的用户图片；过滤工具调用、工具结果、思考过程和系统运行上下文。
保留原会话标题；不沿用源会话的模型、provider 或推理级别。

## 安装

需要 **Python 3.14+**（使用标准库 Zstandard）。

```powershell
git clone https://github.com/Amiyadesi/agent-session-bridge.git
cd agent-session-bridge
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\asb --help
```

下文使用 `asb`；未激活虚拟环境时，Windows 用 `.venv\Scripts\asb`，Linux/macOS 用 `.venv/bin/asb`。
也可以运行 `python -m agent_session_bridge`。
使用 uv 的用户可以运行 `uv tool install git+https://github.com/Amiyadesi/agent-session-bridge.git --python 3.14`。

## 常用命令

### 某个项目的 Codex 会话 → Pi

```powershell
# 预览：只输出 JSON，不写任何文件
asb convert --from codex --to pi --project "D:\work\my-project"

# 复制：写入 Pi 原生会话目录，保留 Codex 源会话
asb convert --from codex --to pi --project "D:\work\my-project" --apply

# 剪切：目标校验后，删除所选源会话文件、对应数据库记录和已知索引项
asb convert --from codex --to pi --project "D:\work\my-project" --move --apply
```

`--project` 按会话记录里的 `cwd` 匹配该目录及其子目录，使用路径边界，不做字符串包含匹配。
迁移前停止源会话；不要剪切仍在写入的会话。

### 单个文件或 ID

```powershell
asb inspect "C:\Users\you\.codex\sessions\2026\10\15\rollout-example.jsonl"
asb convert "C:\Users\you\.codex\sessions\2026\10\15\rollout-example.jsonl" --to pi --apply

asb list --from codex --project "D:\work\my-project"
asb convert --from codex --session SOURCE_UUID --to pi --apply
```

省略 `--from` 时，仅单文件支持自动识别。ID 使用完整精确匹配。

### 自定义源目录、目标目录

```powershell
# 递归读取该目录内的 Codex 日志
asb convert "D:\exports\codex" --from codex --to dsh --apply

# 只生成便携文件，不写入 agent 的默认会话目录
asb convert "D:\exports\codex" --from codex --to pi --output-dir "D:\converted" --apply

# --target-home 指 agent 的 home；未配置 Pi 默认模型时显式指定目标标识
asb convert --from codex --to pi --project "D:\work\my-project" --target-home "D:\portable-pi" --provider TARGET_PROVIDER --model TARGET_MODEL --apply

# 项目已换位置时，指定目标会话的工作目录
asb convert old-session.jsonl --to pi --cwd "D:\new-project" --apply
```

### OpenCode

直接读取和写入初始化过的 OpenCode SQLite 会话库：

```powershell
asb list --from opencode --project "D:\work\my-project"
asb convert --from opencode --session ses_SOURCE_ID --to pi --move --apply
asb convert source.jsonl --to opencode --apply
```

原生 OpenCode 剪切会删除所选 session、message、part 记录；保留其他会话和共享 project。
`--source-root` / `--target-home` 可指定含 `opencode.db` 的目录。
Git 项目需先在目标工作目录启动过 OpenCode，以建立原生项目索引；未建立时会明确报错。
也支持官方便携导出/导入 JSON：

```powershell
opencode export SOURCE_ID > opencode-session.json
asb convert opencode-session.json --from opencode --to pi --apply

asb convert source.jsonl --to opencode --output opencode-import.json --apply
opencode import opencode-import.json
```

明确传入导出 JSON 文件时，源是该文件；剪切该文件不会删除其来源数据库会话。
要剪切数据库中的会话，使用上面的 `--from opencode --session ...`。

### Antigravity CLI（agy）

支持 CLI 的 SQLite 会话，默认 home 是 `~/.gemini/antigravity-cli`。
先由客户端初始化会话库，迁移前关闭该 home 下的 Antigravity 客户端。

```powershell
asb convert --from codex --to antigravity --project "D:\work\my-project" --apply
asb convert --from antigravity --to pi --project "D:\work\my-project" --apply
asb convert --from antigravity --session SOURCE_UUID --to codex --move --apply
```

`--source-root` / `--target-home` 指向含 `conversation_summaries.db` 的 home；
来源也可以是 `conversations` 目录或单个 `.db` 文件。
原生写入会登记标题、项目目录、SQLite 索引、摘要缓存和标题注释文件。
剪切删除所选会话 DB、注释、manifest 及对应缓存/索引项；保留共享库与其他会话。
存在活动客户端锁或未关闭的 WAL/SHM/journal 时拒绝剪切。

返回的 `resume` 是 `agy --conversation NEW_UUID`。便携 `.db` 输出请连同 `.manifest.json`
保存，以保留标题和工作目录；便携文件不登记原生索引，需再次转换到原生 home 后按 ID 恢复。
压缩摘要转为标明来源的用户上下文，转换数量计入 manifest；图片暂不迁移。

**桌面版范围：** 此适配器处理 agy CLI SQLite，尚不直接读写桌面版旧 `.pb` 或加密文件。
桌面会话可先在 agy 的 `/resume` 中按 Tab 切到 **Antigravity** 标签，导入后再转换 CLI 副本。
对该副本执行 `--move` 只删除 CLI 副本，桌面原会话仍需在桌面客户端处理。
本工具尚未接入桌面版原生写入。

### WorkBuddy 与 Codex 数据库

原生目标目录需要先由对应客户端初始化，CLI 不创建或猜测客户端的数据库结构。
支持已验证的 Codex `state_*.sqlite`、WorkBuddy `workbuddy.db`、OpenCode `opencode.db`
和 Antigravity CLI `conversation_summaries.db`；
遇到不兼容结构会明确失败，并保留源会话。

```powershell
asb convert source.jsonl --to workbuddy --apply
asb convert --from workbuddy --session SOURCE_UUID --to codex --move --apply
asb convert source.jsonl --to workbuddy --target-home "D:\portable-workbuddy" --apply
```

WorkBuddy 使用 `WORKBUDDY_CONFIG_DIR` 或已有的 `.workbuddy` / `.workbuddy-ai`；只处理本地会话。
单账号目录沿用该账号 ID；多账号目标须提供 `--workbuddy-user-id ID`。
空库写入客户端支持的未分配账号记录；登录后可见。可显式指定账号以限制归属。
迁移后在 WorkBuddy 会话列表打开。WorkBuddy 图片 blob 暂不迁移，丢弃数量会记录。

### DSH

支持原生 `session.v4.jsonl` 和 `session.v4.jsonl.zstd`；写出带校验和的独立 zstd 帧。
默认目标是 `$DSH_HOME/sessions/<project>/<new-id>/session.v4.jsonl.zstd`。
自定义持久化根目录用 `--source-root` 或 `--target-home`。
旧版日志需先由 DSH 升级为 v4，不猜测旧格式。

```powershell
asb convert "D:\exports\session.v4.jsonl.zstd" --from dsh --to codex --apply
asb convert source.jsonl --to dsh --apply
```

## 数据与删除规则

- 默认预览；`--apply` 才写入。默认复制；`--move --apply` 才剪切。
- 每次生成新 ID，拒绝覆盖已有目标或 manifest。
- Codex 原生来源按 ID 和 rollout 路径核对 DB，优先读取 `name`，其次 `title`；独立导出文件只能保留日志自带的标题。标题写入目标支持的命名记录或原生 DB；Codex 便携文件的标题损失仍会计入 manifest。
- 每个目标旁保存 `.manifest.json`，包含来源、目标、记录统计、丢弃项计数；不包含对话正文。Antigravity 的已写入 manifest 还保存标题，预览仍不输出标题或正文。
- 剪切前：写入文件和 manifest、校验 SHA-256、解析器回读、确认 ID、注册原生索引、再次确认源文件或数据库会话快照没变化。
- 写入或回读失败：清理本次创建的目标文件，保留源文件。批量操作逐项处理；失败返回非零退出码，已成功项目保留。
- 原生剪切按所选 ID 删除数据库行、关联会话记录和已知列表/历史缓存；保留数据库本身、其他会话、共享项目、附件 blob 和备份。
- 文件重命名、索引改写和数据库事务配合回滚。目标成功后源清理失败时保留目标与源，并返回错误；先检查结果再重试。进程被强制终止或断电时，遗留的 `.asb-cut-*` 文件需要人工恢复。
- DSH 有多个历史 generation 时拒绝剪切，避免删完最新版又显示旧版；复制仍可用。
- 不迁移 auth、API key、hooks、MCP、权限配置、签名思考链或运行中的工具状态。
- 复制和剪切都只迁移对话上下文。工具调用、工具结果、思考过程、系统消息和运行元数据过滤掉，数量计入 manifest 的 `filtered:*`。
- 支持原生压缩的目标保留上下文边界；Pi 续聊使用最新摘要及其后的消息，旧对话仍在会话历史中。不可表示的数据计入 manifest。DSH、Antigravity 的摘要转换为明确标注的用户上下文，图片暂不迁移。
- 源模型与 provider 在序列化前清空，源推理设置随运行记录过滤；manifest 记录 `filtered:session_model` / `filtered:session_model_provider`。
- Pi 会从历史助手消息恢复模型，无法只删除 `model_change`。迁移改用目标 home 的 `settings.json` 默认模型，目标 cwd 的 `.pi/settings.json` 优先；`--provider` / `--model` 可覆盖。没有完整目标默认值时，必须显式指定目标 provider 和 model，避免导入后恢复源模型或 `unknown`。
- 其他格式中的必需历史模型字段使用格式占位值或显式目标标识；不会继承源标识。这些字段不创建 provider 配置，目标客户端仍需原有登录或本地配置。
- 转换文件可被解析，不等于已在该客户端调用模型续聊。当前证据见 [兼容性](docs/compatibility.md)。
- 目标文件系统需支持硬链接（例如 NTFS、ext4）；不支持时写入失败，保留源文件。

**便携输出目录**里的 Codex / Claude / Qwen / DSH 文件不会自动出现在原生客户端列表；默认原生目录或 `--target-home` 才按客户端布局保存。Pi 可直接 `pi --session <path>` 打开便携文件。
OpenCode / WorkBuddy / Antigravity 的便携输出也不注册数据库。

## 真实会话验证

Windows 上用本地真实 Codex、Pi 会话验证了 **98 条格式转换路线**，以及真实对话副本的 **49 条原生剪切路线**。
Codex / Pi / OpenCode / Claude 官方 SDK / WorkBuddy 原生读取通过；DSH v4 恢复器通过，Qwen 原生列表发现导入会话。
另对用户指定的真实 Codex 会话完成了剪切，验证其 transcript、数据库记录和已知缓存已移除，其他会话 ID 保持不变。
测试没有向模型发送请求，真实会话不作为仓库 fixture 发布。详见 [版本和验证层级](docs/compatibility.md)。

新增 Antigravity 后，另用真实 Codex、Pi 副本验证 **30 条涉及 Antigravity 的转换路线**，
其中 **16 条原生剪切**检查了 DB、标题注释、摘要缓存和邻居会话保留。
Windows Antigravity CLI 1.4.0 原生恢复和续聊落盘通过；使用本机 HTTP 模拟响应，
未向外部模型发送私人对话，恢复后原标题和工作目录保持一致。

## AI Skill

[`skills/migrate-agent-session/SKILL.md`](skills/migrate-agent-session/SKILL.md) 与 CLI 共用同一套逻辑。
将 `skills/migrate-agent-session` 链接到所用 agent 的项目 skill 目录；不需要复制转换代码。

给 AI 的请求示例：

> 用 migrate-agent-session，把 `D:\work\my-project` 的 Codex 会话复制到 Pi。先预览匹配数量和损失，再执行；保留原会话。

> 把这个会话剪切到 DSH：`D:\exports\one.jsonl`。目标确认可读后删除这个原文件。

## 开发

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
```

开发前安装 `python -m pip install -e ".[dev]"`。CI 在 Windows / Linux 运行单元测试；原生客户端验证记录来自 Windows。

## 社区

感谢 [LINUX DO](https://linux.do/) 社区的交流与分享，认可社区对开源与互助的支持。
本项目由社区成员独立维护。欢迎提交 issue、格式样本说明和适配 PR；不要公开上传私人会话或凭据。
推广短文及社区声明模板见 [docs/promotion.zh-CN.md](docs/promotion.zh-CN.md)。

源码与选型依据：[docs/compatibility.md](docs/compatibility.md)。

MIT。底层格式适配器版权归原作者；本仓库通过依赖使用它们。
