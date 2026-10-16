# 格式依据与验证边界

验证日期：2026-10-16，Windows / Python 3.14.3。运行依赖固定为 MIT 项目 `session-migrate==0.11.0`。

## 已验证的客户端

| Agent | 存储与迁移 | 本次原生检查 |
| --- | --- | --- |
| Codex CLI 0.162.1 | rollout JSONL + `state_5.sqlite` 的 threads / 关联行 | 隔离 app-server `thread/resume`、`thread/read` 返回真实迁移对话；未发送 `turn/start` |
| Pi 0.87.1 | v3 JSONL 会话树，当前分支、压缩摘要 | `SessionManager.open()`、`buildSessionContext()` 读取真实迁移对话 |
| DSH 0.2.0-rc.2 | v4 JSONL，独立 checksummed zstd frames | 发布包 v4 `restoreReleasedV4Artifact` 接受真实对话日志；未启动完整交互 UI |
| OpenCode 1.19.1 | `opencode.db` 的 project / session / message / part；兼容官方 JSON bundle | 发布版 CLI `export` 从桥接写入的 DB 读出完整真实对话 |
| Qwen Code 0.25.2 | projects 下的 ChatRecord JSONL | `sessions list --json` 发现两个真实导入会话；完整历史已通过适配器回读，未验证模型续聊 |
| Claude Code 2.1.301 | projects 下的 JSONL transcript | 官方 Claude Agent SDK 0.3.301 的 `listSessions`、`getSessionMessages` 读取真实导入对话；未执行交互续聊 |
| WorkBuddy 5.7.6.40488862 | `projects/<slug>/<id>.jsonl` + `workbuddy.db` + 最近会话缓存 | 官方发布包原生数据库 repository 的 list/get、历史 `resolveReplayHistory` 读取真实导入；未登录 GUI 或调用模型 |

以上检查区分 **格式解析、原生读取、模型续聊**。本次没有向任何模型发送请求，不承诺所有未来客户端版本兼容。
Claude Code 和 WorkBuddy 是额外兼容的闭源客户端；本仓库自身完整使用 MIT 协议开源，不分发这些客户端。

## 真实数据验证结果

- 两份本地真实来源：Codex、Pi。分别转换到七种格式，再将七种结果各转到七种目标，合计 **98 条转换路线**通过解析和过滤检查。
- 使用真实对话的副本验证 **7 × 7 = 49 条原生目录剪切路线**。数据库来源按 ID 删除所选记录；其余会话 ID 集合保持不变。OpenCode 的 message/part 和桥接导出文件一并清理。
- 两份来源的导入目标分别得到 9 条和 33 条上下文消息；原生读取未包含工具执行或思考记录。Pi 默认迁移当前活动分支，未选中的分支记录计入丢弃项。
- 用户明确指定的一条真实 Codex 会话完成实际剪切，Pi 原生读取了 9 条对话消息；原 transcript、threads 行、关联记录和已知索引项已删除。其他 Codex ID 和指定保留的 Pi 文件未变。
- 另一次已保留的真实 Codex → Pi 复制：353 条用户/助手消息、29 个摘要；文本和摘要序列 hash 一致，原文件 hash 未变。Pi 根据最新摘要构建续聊上下文。
- 标题/模型修复后，同一份 Pi 目标保留原 ID，原生 `getSessionName()` 返回原 Codex 标题；353 条消息、29 个摘要保持不变，5 条有效续聊消息的内容 hash 一致。原生上下文模型改为目标 Pi 默认值，源 Codex 文件 hash 未变；桥接 manifest 的目标 hash 同步更新。额外启动已安装 Pi 的 RPC `get_state`，确认原标题、目标默认模型、5 条上下文且未 streaming；启动输出未出现模型恢复警告。仍未调用模型。
- 单元测试使用不含私人信息的合成 fixtures；真实原生验证在忽略目录执行，私人对话不进入仓库。

## 数据库与删除规则

- 客户端负责初始化 schema；桥接只连接已有 DB，不建一份猜测结构的原生库。
- 目标原生模式写 transcript，并在 SQLite 事务中注册会话与所需索引。新增的未知必需列会导致明确失败。便携输出不注册 DB。
- Codex 原生源根据 rollout 所属 home 找到 state DB，并核对 ID 与 rollout_path。删除匹配 threads 行、FK 子行以及 thread_spawn_edges；清理 session_index/history 中对应 ID。
- WorkBuddy 仅迁移本地会话；写 sessions、workspaces。剪切核对规范 transcript 路径，删除 sessions、session_usage、所选 automation run/outbox 记录，更新 app/sessions.json；共享工作区和附件保留。
- WorkBuddy 多账号目标需要 `--workbuddy-user-id`。空库使用客户端支持的空 user_id。源清理按精确 ID 与路径匹配，不猜账号。
- OpenCode 仅支持本次已验证的 session/message/part schema。开发分支已有不同的 session_v2 结构，桥接会拒绝该结构，不能按旧表名静默导入。
- 原生 OpenCode 来源通过 DB 快照映射为虚拟路径 `.../opencode.db/<session-id>`；剪切删除的是 DB 会话。显式输入官方 export JSON 时只操作该文件。
- 数据库事务配合源文件临时重命名、缓存原子替换。普通异常会回滚；源后续清理失败时保留已完成的目标并报告错误。多个文件和 DB 之间不具备跨进程、断电级原子性；停止源客户端后再剪切。
- Claude 已知 sessions-index/history、桥接自己的 manifest 随选中会话清理。未识别的第三方缓存、备份和共享附件不递归删除。

## 格式与损失

- 默认保留用户/助手文本、摘要及目标可表示的用户图片。工具调用/结果、思考、系统与运行记录在序列化前过滤，`filtered:*` 合并进 manifest 丢弃计数。
- Codex 原生标题从按 ID、rollout_path 匹配的 threads 行读取，优先 name，再 title；未命中时保留日志标题。Pi 写 session_info，DSH/Claude/Qwen/WorkBuddy 写各自标题记录，OpenCode 写 session.title，Codex 原生目标写 threads 的 title/name。Codex 便携输出仍由上游适配器计入 session:title 损失。
- 源 Session.model / model_provider 清空后才传入序列化器。Pi 0.87.1 会从每条历史助手消息恢复模型，即使不存在 model_change；完全删字段仍会产生无效恢复目标。因此 Pi 使用目标 home/settings.json 和 cwd/.pi/settings.json 的默认 provider/model，显式 CLI 标识优先；无完整目标值时在写入前失败。源 thinking_level_change 被过滤，续聊推理设置由 Pi 自身决定。其他格式的必需历史字段使用格式占位值或显式目标标识。
- manifest 的来源摘要、SHA-256 对应原文件；数据库来源使用所选会话的稳定 JSON 快照，不是整个数据库文件 hash。
- Pi 的额外 system/bashExecution/custom 运行记录经过边界适配后过滤；分支/压缩摘要保留，未选中的分支不重新加入上下文。
- DSH 写入借用 Pi 的对话归并，再建立 turn/step 生命周期；摘要通过 surface replacement 更新模型上下文。DSH、WorkBuddy 暂不迁移图片 blob。
- DSH 多个物理 generation 时拒绝剪切，避免旧版复活；桥接 manifest 不作为 generation。未知必需事件、损坏日志和不支持版本明确失败，不截断修复。
- 不迁移账号、密钥、权限配置、MCP、hooks、工具运行状态和签名思考链。目标模型/provider 需要客户端自己的配置。
- 文件写入拒绝覆盖；原子创建需要目标文件系统支持硬链接（如 NTFS、ext4）。Windows 继承父目录 ACL；POSIX 临时文件使用私有权限。

## 来源与选型

优先复用现有事件模型；针对数据库采用已安装发布版 schema 和原生读回校验。
只复制 transcript 不能覆盖本次用户要求的原生索引与剪切，因此增加了范围明确的 SQLite 操作。

| 来源 | 用途 |
| --- | --- |
| [session-migrate](https://github.com/xhluca/session-migrate/tree/a3b0fae4a38bbe0c0c89876f2f2ee6ee0ad154c4) | MIT 的 Codex / Pi / OpenCode / Qwen / Claude 解析与序列化，发布依赖固定 0.11.0 |
| [Codex app-server 文档](https://developers.openai.com/codex/app-server/) | 原生 resume/read 验证接口 |
| [Pi 0.87.1 session-manager](https://github.com/earendil-works/pi-mono/blob/f07218c4d4bbc12bef056a7058c3dd49dfe41abe/packages/coding-agent/src/core/session-manager.ts) | session_info 名称与历史助手消息的模型恢复规则；与本机 0.87.1 原生读取交叉核对 |
| [Pi 0.87.1 SDK](https://github.com/earendil-works/pi-mono/blob/f07218c4d4bbc12bef056a7058c3dd49dfe41abe/packages/coding-agent/src/core/sdk.ts) / [settings-manager](https://github.com/earendil-works/pi-mono/blob/f07218c4d4bbc12bef056a7058c3dd49dfe41abe/packages/coding-agent/src/core/settings-manager.ts) | 恢复失败警告、默认模型/推理设置，以及全局和项目 settings.json 的路径与优先级 |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/86a402200d95fecef4529d969836f1fe13c6cb0d) | v4 codec、范围引用、surface replacement 与生命周期；以下载发布包的恢复器验证 |
| [OpenCode v1.19.1 import](https://github.com/anomalyco/opencode/blob/v1.19.1/packages/opencode/src/cli/cmd/import.ts) | 官方 bundle 到 session/message/part 的写入规则 |
| [OpenCode v1.19.1 export](https://github.com/anomalyco/opencode/blob/v1.19.1/packages/opencode/src/cli/cmd/export.ts) | DB 原生读回验证 |
| [OpenCode 开发分支的重构](https://github.com/anomalyco/opencode/tree/d7d44a7e03a9a390a3b66d1da8bb59aa03b65f9e/packages/core/src/session) | 识别 session_v2 版本差异，避免混写 |
| [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-typescript) | 官方会话列表与消息读取函数，使用发布版 0.3.301 |
| [WorkBuddy 官网](https://www.workbuddy.ai/) | 官方 Windows 发布包 5.7.6.40488862；从发布包只读核对迁移 SQL、路径规则和历史回放 |
| [Scoop WorkBuddy 分发说明](https://github.com/ScoopInstaller/Extras/blob/master/bucket/workbuddy.json) | 官方安装包地址、解包结构和 SHA-256 交叉核对 |

WorkBuddy 官方安装包 SHA-256：`b4ed6c44dcfb480318cdf1fbb4874545a56403dac87c8f9b9d6ac451e88d8f15`。
未执行网上的第三方迁移 skill，也未将客户端二进制或闭源代码放入本仓库。

研究过的替代方案：[UniSessions](https://github.com/kyle0r/ai-agent-session-export) 提供会话导出与 SDK；
[dsh-import-agents](https://github.com/Chang-Tong/dsh-import-agents) 提供 DSH 导入。
本项目选择固定适配器加本地 DSH/数据库扩展，以实现统一双向转换和 Windows 剪切。
