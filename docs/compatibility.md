# 格式依据与验证边界

验证日期：2026-10-17，Windows / Python 3.14.3。运行依赖固定为 MIT 项目 `session-migrate==0.11.0`。

## 已验证的客户端

| Agent | 存储与迁移 | 本次原生检查 |
| --- | --- | --- |
| ZCode CLI / 桌面 | `.zcode/cli/db/db.sqlite` session/message/part/session_entry 与 `.zcode/v2/tasks-index.sqlite`；读取 sequence、回退分支、压缩边界 | 上游固定 commit `aac4755666d09fdcd70272fcf063c077a639015f`（源码产品版本 3.15.1、存储版本 0.16.5）；用本机 3.12.3 数据库结构的空隔离库验证写入和剪切。官方 getSession/listSessions/messages 模块通过 Node 24.14.0 读取真实迁移内容；仅编译 TypeScript 和重定向 import，未改读取逻辑。未运行发布版 GUI 或模型续聊 |
| Codex CLI 0.162.1 | rollout JSONL + `state_5.sqlite` 的 threads / 关联行 | 隔离 app-server `thread/resume`、`thread/read` 返回真实迁移对话；未发送 `turn/start` |
| Pi 0.87.1 | v3 JSONL 会话树，当前分支、压缩摘要 | `SessionManager.open()`、`buildSessionContext()` 读取真实迁移对话 |
| DSH 0.2.0-rc.2 | v4 JSONL，独立 checksummed zstd frames | 发布包 v4 `restoreReleasedV4Artifact` 接受真实对话日志；未启动完整交互 UI |
| OpenCode 1.19.1 | `opencode.db` 的 project / session / message / part；兼容官方 JSON bundle | 发布版 CLI `export` 从桥接写入的 DB 读出完整真实对话 |
| Qwen Code 0.25.2 | projects 下的 ChatRecord JSONL | `sessions list --json` 发现两个真实导入会话；完整历史已通过适配器回读，未验证模型续聊 |
| Claude Code 2.1.301 | projects 下的 JSONL transcript | 官方 Claude Agent SDK 0.3.301 的 `listSessions`、`getSessionMessages` 读取真实导入对话；未执行交互续聊 |
| WorkBuddy 5.7.6.40488862 | `projects/<slug>/<id>.jsonl` + `workbuddy.db` + 最近会话缓存 | 官方发布包原生数据库 repository 的 list/get、历史 `resolveReplayHistory` 读取真实导入；未登录 GUI 或调用模型 |
| Antigravity CLI 1.4.0 Windows x64 | `conversations/<uuid>.db` + SQLite 摘要索引、protobuf 列表缓存、标题注释 | 官方发布包恢复真实 Pi 对话；本机 Gemini HTTP 模拟服务收到历史并返回固定响应，原生续聊落盘、原标题、工作目录回读通过；没有外部模型推理 |
| Antigravity IDE 2.5.5 Windows x64 | 桌面 SQLite `conversations/<uuid>.db` + `state.vscdb` 两个历史索引、已有 protobuf 缓存、标题注释 | 官方发布包语言服务 `GetCascadeTrajectory` 按 ID 回读真实迁移对话，逐条核对角色/正文、原标题和目录；回读后 `GetAllCascadeTrajectories` 可列出该 ID，未验证 GUI 历史列表或模型续聊 |

以上检查区分 **格式解析、原生读取、模型续聊**。Antigravity 的续聊检查使用本机模拟服务，其他原生检查未发送对话轮次；均未向外部模型发送私人对话。不承诺所有未来客户端版本兼容。
Claude Code、WorkBuddy 和 Antigravity 是额外兼容的闭源客户端；本仓库自身完整使用 MIT 协议开源，不分发这些客户端。

## 真实数据验证结果

- ZCode 增加 **83 次真实会话转换与 20 次原生剪切**：真实 Codex、Pi 来源各覆盖十种来源格式到 ZCode、ZCode 到十种目标；另从本机真实 ZCode 会话导出 Pi（29 条有效上下文）。测试使用隔离副本，两个真实源文件 SHA-256 不变，真实 ZCode 所选会话快照 hash 不变。
- ZCode 原生剪切精确清理所选会话及已有桌面索引；检查 session/message/part/session_entry/input_history 已删除、其他 session ID 集合不变。单元测试另覆盖用量、session 设置、任务分组引用和双库异常回滚。
- 官方存储模块从隔离 ZCode 数据库回读 Codex 的 **413 条历史记录**（381 条对话、32 条摘要）及 Pi 的 **31 条记录**，角色/文本序列 hash 一致、原标题保留、无历史模型/provider 选择。Codex 的最新摘要与其后消息构成 7 条有效上下文。官方查询按目标 directory 发现新 ID；桌面 GUI 是否显示、完整 runtime hydration 和模型续聊未测试。

- 两份本地真实来源：Codex、Pi。分别转换到七种格式，再将七种结果各转到七种目标，合计 **98 条转换路线**通过解析和过滤检查。
- 使用真实对话的副本验证 **7 × 7 = 49 条原生目录剪切路线**。数据库来源按 ID 删除所选记录；其余会话 ID 集合保持不变。OpenCode 的 message/part 和桥接导出文件一并清理。
- 两份来源的导入目标分别得到 9 条和 33 条上下文消息；原生读取未包含工具执行或思考记录。Pi 默认迁移当前活动分支，未选中的分支记录计入丢弃项。
- 用户明确指定的一条真实 Codex 会话完成实际剪切，Pi 原生读取了 9 条对话消息；原 transcript、threads 行、关联记录和已知索引项已删除。其他 Codex ID 和指定保留的 Pi 文件未变。
- 另一次已保留的真实 Codex → Pi 复制：353 条用户/助手消息、29 个摘要；文本和摘要序列 hash 一致，原文件 hash 未变。Pi 根据最新摘要构建续聊上下文。
- 标题/模型修复后，同一份 Pi 目标保留原 ID，原生 `getSessionName()` 返回原 Codex 标题；353 条消息、29 个摘要保持不变，5 条有效续聊消息的内容 hash 一致。原生上下文模型改为目标 Pi 默认值，源 Codex 文件 hash 未变；桥接 manifest 的目标 hash 同步更新。额外启动已安装 Pi 的 RPC `get_state`，确认原标题、目标默认模型、5 条上下文且未 streaming；启动输出未出现模型恢复警告。仍未调用模型。
- 单元测试使用不含私人信息的合成 fixtures；真实原生验证在忽略目录执行，私人对话不进入仓库。
- Antigravity 新增 **30 条真实上下文路线**：真实 Codex、Pi 来源各覆盖七种格式到 Antigravity、Antigravity 到八种格式。后者共 **16 条原生剪切**，检查所选 DB、标题注释、manifest、protobuf 缓存项删除及其他会话 ID 不变；两个真实源文件 SHA-256 均不变。
- 另用真实 Pi 迁移副本在 Windows CLI 1.4.0 续聊：本机服务收到历史，客户端持久化固定响应。恢复前后的标题、工作目录一致，源 hash 不变。该检查验证原生历史请求与写回，不验证真实供应商认证、模型推理或桌面 GUI。
- 桌面格式另覆盖 **34 条真实上下文路线**：两份 Codex/Pi 来源各验证桌面到九种格式、其余八种格式到桌面；其中 **18 条原生剪切**检查两个 SQLite 索引、两个已知 protobuf 缓存、注释及 manifest，邻居会话和无关设置保留，原始来源 SHA-256 不变。
- 官方 IDE 2.5.5 语言服务按 ID 回读三份真实副本，分别 **395、31、382 条**；逐条角色/文字完全一致，目录和有效标题一致（无原标题时使用默认导入名称），三个保留源文件的 SHA-256 均未变。未发送新一轮消息。

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
- Antigravity CLI：每会话 DB 剪切后移除，按 ID 删除 `conversation_summaries` 行、`jetbox_summaries_proto.pb` 对应 map 项、`cache/last_conversations.json` 对应值和 `annotations/<id>.pbtxt`。共享索引库、其他 ID、brain 工件/日志及备份保留。缓存替换与 SQLite 事务协作回滚；活动客户端 presence 锁或会话 WAL/SHM/journal 阻止原生修改/剪切，读取和复制仍支持一致的 WAL 快照。
- Antigravity 桌面：默认数据目录 `~/.gemini/antigravity-ide`，旧 SQLite 数据可在 `~/.gemini/antigravity`。共享状态库位于平台配置目录的 `Antigravity IDE/User/globalStorage/state.vscdb`（旧目录的应用名为 `Antigravity`）。自定义位置通过 `--source-state-db` / `--target-state-db` 指定；不创建一份假状态库代替客户端初始化。
- 桌面更新 `ItemTable` 的 `antigravityUnifiedStateSync.trajectorySummaries`（base64 protobuf map）与 `chat.ChatSessionStore.index`（version 1 JSON）。剪切按完整 ID 清理这两处索引，以及存在的 `jetbox_summaries_proto.pb` / `agyhub_summaries_proto.pb`、注释、会话 DB 和 manifest。共享状态库、配置、其他 ID、brain/附件/备份保留。活动 daemon PID、状态库或会话 sidecar 阻止修改；同 ID 的 `.pb` 与 `.db` 并存时拒绝剪切。

## Antigravity 的格式边界

- 编解码复用固定依赖的 Antigravity CLI 1.1.16 SQLite/protobuf 子集；原生集成另以官方 Windows **1.4.0** 发布包核验。未改动依赖的可执行文件哈希 gate，也未调用其仅支持 Linux 1.1.16 的安装器。
- 写入使用已观测到的 SQLite `user_version=1`。缺失该标记会让客户端运行旧格式迁移，清空导入的 trajectory 元数据；真实恢复验证覆盖了这一差异。
- 工作目录写入 `CortexTrajectoryMetadata.workspaces`（1）和 `workspace_uris`（7）；用 `Path.from_uri` 读取 SQLite 索引 URI，正确处理 Windows 盘符。1.4.0 索引新增可空 `raw_summary` 和带默认值的 `group_id`；未知必需字段拒绝写入。
- `CascadeTrajectorySummary` 列表缓存写入标题注释、时间、步数、trajectory ID 和工作目录元数据；原标题同时写入权威的 `annotations/<conversation-id>.pbtxt`。只写 SQL 标题会在原生恢复后丢失，真实客户端检查已覆盖。
- 固定依赖的 SQLite context manager 不关闭连接，导致 Windows 临时 DB 无法删除。本地仅给该 codec 的 SQLite binding 使用退出时关闭的 Connection；其余格式和 schema/protobuf 校验保持原行为。
- 独立 `.db` 本身不保存 UI 标题。桥接只在 apply 后的 manifest 保存标题和 cwd；预览保持无正文、无标题。移动便携输出时需同时保留该 manifest。
- 没有原生压缩边界的目标将摘要改为标注的用户上下文，计入 `compaction:converted_to_user_context`；图片、工具、思考和运行状态不写入。
- `antigravity-desktop` 单独处理 IDE 2.5.5 SQLite。官方 Windows 发布包初始化的 schema 与固定 codec 一致；桌面 `trajectory_type=4, source=1`，CLI 为 source 17。只在内存快照适配枚举和正文结构以复用现有校验/投影；来源 SHA 仍对应原始快照。写入移除 CLI 专属 project ID，并保留工作目录和标题注释。
- 桌面分段用户文本按 `CortexStepUserInput.items` → `TextOrScopeItem.text` 结构读取；不猜测任意可读字符串，不复制其中的文件/运行上下文。编辑后的助手回复使用 `modified_response`。其余已知工具/思考记录仍在统一边界过滤，不支持的编码明确失败。
- 官方语言服务使用隔离配置、禁止外部推理的端点进行检查。冷启动的 `GetAllCascadeTrajectories` 在本次 standalone 环境返回空列表；指定 ID 读取后可列出会话及正确标题。**该证据不能证明 GUI 冷启动历史发现或模型续聊已通过。** 桌面 `resume` 返回空数组，不伪造 agy 恢复命令。
- 旧 `.pb` 与加密历史不直接转换，需先由官方桌面客户端升级/导入为 SQLite；或使用官方 CLI `/resume` 的 **Antigravity** 标签克隆到 CLI 后再迁移。剪切 CLI 副本不会删除桌面原会话。

## 格式与损失

- 默认保留用户/助手文本、摘要及目标可表示的用户图片。工具调用/结果、思考、系统与运行记录在序列化前过滤，`filtered:*` 合并进 manifest 丢弃计数。
- Codex 原生标题从按 ID、rollout_path 匹配的 threads 行读取，优先 name，再 title；未命中时保留日志标题。Pi 写 session_info，DSH/Claude/Qwen/WorkBuddy 写各自标题记录，OpenCode 写 session.title，Codex 原生目标写 threads 的 title/name。Codex 便携输出仍由上游适配器计入 session:title 损失。
- 源 Session.model / model_provider 清空后才传入序列化器。Pi 0.87.1 会从每条历史助手消息恢复模型，即使不存在 model_change；完全删字段仍会产生无效恢复目标。因此 Pi 使用目标 home/settings.json 和 cwd/.pi/settings.json 的默认 provider/model，显式 CLI 标识优先；无完整目标值时在写入前失败。源 thinking_level_change 被过滤，续聊推理设置由 Pi 自身决定。其他格式的必需历史字段使用格式占位值或显式目标标识。
- manifest 的来源统计、SHA-256 对应原文件；OpenCode 数据库来源使用所选会话的稳定 JSON 快照，Antigravity 使用包含 WAL 的一致 SQLite 备份快照。不会用共享索引 DB 的整体 hash 代替单个会话校验。
- Pi 的额外 system/bashExecution/custom 运行记录经过边界适配后过滤；分支/压缩摘要保留，未选中的分支不重新加入上下文。
- DSH 写入借用 Pi 的对话归并，再建立 turn/step 生命周期；摘要通过 surface replacement 更新模型上下文。DSH、WorkBuddy 暂不迁移图片 blob。
- DSH 多个物理 generation 时拒绝剪切，避免旧版复活；桥接 manifest 不作为 generation。未知必需事件、损坏日志和不支持版本明确失败，不截断修复。
- 不迁移账号、密钥、权限配置、MCP、hooks、工具运行状态和签名思考链。目标模型/provider 需要客户端自己的配置。
- 文件写入拒绝覆盖；原子创建需要目标文件系统支持硬链接（如 NTFS、ext4）。Windows 继承父目录 ACL；POSIX 临时文件使用私有权限。

## 来源与选型

### ZCode

ZCode 本地 codec 复用 Pi 的消息归并，使用带 `format=zcode` / `schema_version=1` 的桥接 JSON，不冒充官方导入格式。
调用固定引擎前仅映射为 OpenCode 枚举，manifest 保留 ZCode 来源身份。便携文件剪切只删除文件，原生 ID 选择才清理数据库。

读取 `sequence IS NULL, sequence, time_created, rowid` 的消息顺序及各 message 内的 part sequence；
导出采用官方 compact-first / branch-cut 语义，保留有效分支、最新摘要和 preserved segment。
不读附件引用、不复制 blob，忽略模型/权限/session_entry 运行状态并计数。
目标标题写 `title_source=custom` 和桌面 `title_overridden`；桌面 `provider=glm` 表示产品 runtime，未写历史模型选择。

原生 home 必须已初始化。会话库和已有桌面库通过 SQLite ATTACH 在同一事务内修改，普通异常回滚；
WAL 模式不承诺断电时跨库原子性。剪切清理所选输入历史、session 设置、用量和精确 task/group 引用；
排队输入、运行中用量、子会话、工作流、自动化引用会阻止剪切。
共享分组、项目设置、附件、调试 rollout、exec 文件和备份保留。

以下均固定于 commit `aac4755666d09fdcd70272fcf063c077a639015f`：

| 来源 | 用途 |
| --- | --- |
| [SQLite schema](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/apps/zcode-cli/packages/adapters/src/storage/session-store/migrations.ts) / [消息 repository](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/apps/zcode-cli/packages/adapters/src/storage/session-store/repositories/messages.ts) / [codec](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/apps/zcode-cli/packages/adapters/src/storage/session-store/codecs.ts) | 官方表结构、顺序、用户旧 model 空对象及模型字段读取依据；本机数据库结构交叉核对 |
| [History hydration](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/apps/zcode-cli/packages/core/src/agent/session-history-hydrator.ts) / [compact](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/apps/zcode-cli/packages/core/src/agent/compact-session.ts) / [rewind](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/apps/zcode-cli/packages/contracts/src/rewind/index.ts) | 有效分支、摘要和压缩保留段算法 |
| [Desktop task index](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/packages/services/src/session/taskIndexRepo.ts) / [index syncer](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/packages/services/src/zcode-agent/zcodeTaskIndexSyncer.ts) / [project ID](https://github.com/zai-org/ZCode/blob/aac4755666d09fdcd70272fcf063c077a639015f/apps/zcode-cli/packages/bootstrap/src/app/paths.ts) | task meta_json、glm 产品 provider、custom 标题、分组引用、workspace/project 键和双库事务依据 |

### 其他客户端

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
| [Antigravity CLI 官方会话说明](https://antigravity.google/docs/cli/conversations) / [resume](https://antigravity.google/docs/cli/commands/resume) | 工作区作用域、CLI 会话目录与桌面会话克隆入口 |
| [Antigravity CLI 1.4.0 官方发布](https://github.com/google-antigravity/antigravity-cli/releases/tag/1.4.0) | Windows 包运行时核对 SQLite version、摘要字段与 annotations 路径；发布包内 protobuf 描述及原生写回交叉验证 wire 字段 |
| [固定 Antigravity codec 源码](https://github.com/xhluca/session-migrate/blob/a3b0fae4a38bbe0c0c89876f2f2ee6ee0ad154c4/src/session_migrate/formats/antigravity.py) / [原生测试](https://github.com/xhluca/session-migrate/blob/a3b0fae4a38bbe0c0c89876f2f2ee6ee0ad154c4/tests/test_antigravity_native.py) | SQLite/protobuf 基础 schema、损失计数和离线 Gemini 原生验证方法 |
| [Antigravity IDE 官方下载](https://antigravity.google/download) | Google LLC 签名有效的 Windows 2.5.5 发布包；只读核对 schema、内嵌 protobuf descriptors、桌面目录和 ChatSessionStore；原生语言服务隔离回读 |
| [Antigravity Database Manager](https://github.com/ag-donald/Antigravity-Database-Manager/tree/7ce65e1e3ed3bca7eeaf653e5ffd0b8f3482e199) | `src/core/db_operations.py`、`protobuf.py`、`docs/schema.proto` 提供两个 ItemTable 历史索引和按 ID 增删的来源证据；未知结构仍拒绝写入 |
| [Antigravity Storage Manager](https://github.com/unchase/antigravity-storage-manager/blob/450445043226f19761ca777d9fa84ba1874d2fbd/src/ideState.ts) | 交叉核对桌面 state.vscdb 路径；仅重建 brain/task 索引不足以证明真实 DB 会话注册 |
| [Scoop WorkBuddy 分发说明](https://github.com/ScoopInstaller/Extras/blob/master/bucket/workbuddy.json) | 官方安装包地址、解包结构和 SHA-256 交叉核对 |

WorkBuddy 官方安装包 SHA-256：`b4ed6c44dcfb480318cdf1fbb4874545a56403dac87c8f9b9d6ac451e88d8f15`。
Antigravity CLI 1.4.0 Windows x64 ZIP SHA-256：`bcb0af25a19da704f9b70f1fd84a983e49e81b70534d32c4bf14328e4f2ead9b`，与官方 release asset digest 核对。
Antigravity IDE 2.5.5 Windows 安装包 SHA-256：`a8c25631fd5e43bf217a6cf510ca796441817f4ad9c50b1d892282c7366edc4a`；Authenticode 签名为 Google LLC，状态 Valid。
未执行网上的第三方迁移 skill，也未将客户端二进制或闭源代码放入本仓库。

研究过的替代方案：[UniSessions](https://github.com/kyle0r/ai-agent-session-export) 提供会话导出与 SDK；
[dsh-import-agents](https://github.com/Chang-Tong/dsh-import-agents) 提供 DSH 导入。
本项目选择固定适配器加本地 DSH/数据库扩展，以实现统一双向转换和 Windows 剪切。
