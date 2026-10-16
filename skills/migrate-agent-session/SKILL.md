---
name: migrate-agent-session
description: Copy or cut conversation context between Codex, Pi, DeepSeek Harness, OpenCode, Qwen Code, Claude Code, WorkBuddy, Antigravity CLI (agy) and desktop IDE with the local agent-session-bridge CLI. Supports a transcript, exact session ID or project batch, including native database indexes; excludes credentials and agent configuration.
---

# Migrate agent sessions

Use `asb` / `agent-session-bridge`; JSON stdout is the machine interface.
The CLI uses local files, makes no model calls and never uploads transcripts.
Copy and move include conversation context only: user/assistant messages, compaction summaries and supported user images. Tool calls/results, thinking and system/runtime records are filtered automatically. Counts appear as `filtered:*` in the manifest; no extra flag is needed.
Preserve the source title where the target supports it. Native Codex titles come from the matching DB row (`name`, then `title`); standalone exports need a title in the log. Source model/provider and thinking settings are excluded.
Use an installed command or the checkout's `.venv/Scripts/asb.exe` (Windows) / `.venv/bin/asb` (POSIX).
If not installed, this project needs Python 3.14+; create a project venv and run `python -m pip install -e .` there.

## Select the sessions

- Single file: `asb inspect <file>` then `asb convert <file> --to pi`.
- Exact ID: `asb convert --from codex --session <full-id> --to pi`.
- Project: `asb list --from codex --project <absolute-directory>`, then `asb convert --from codex --to pi --project <absolute-directory>`.
- Export directory: `asb convert <directory> --from codex --to pi`.
- A project filter matches the recorded cwd and descendants by path boundary. It does not search message text.
- `--source-root` overrides a source store; `--target-home` is the target agent home (not its `sessions` directory).
- Use `--cwd` only when the user intends to relocate the working directory.
- Pi requires a destination model because it restores model selection from historical assistant messages. The CLI reads defaults from the target home `settings.json`, then target cwd `.pi/settings.json`; `--provider` / `--model` override them. With no complete target defaults, supply the intended target pair. Never substitute the source model. No settings files are changed.

Supported names: `codex`, `pi`, `dsh`, `opencode`, `qwen`, `claude`, `workbuddy`, `antigravity`, `antigravity-desktop`.
Use `asb formats` / `asb convert --help` for available options.

## Execute

Run without `--apply` first. Read `count`, `errors`, `source`, `target`, `dropped_events` and `warnings`.
Report the selected set and material losses without printing conversation bodies.
Resolve unexpected matches or unsupported content before performing a destructive move.

- User asks to copy, or leaves retention unspecified: append `--apply`; retain sources.
- User explicitly asks to move/cut/delete those source sessions after migration: append `--move --apply`.
- Preserve already-given authorization. Ask only if deletion intent or the selected set is unclear.
- Stop active source conversations before a move. The CLI rejects sources changed during conversion, but cannot stop another client from writing.
- Never substitute manual `rm`, `del` or recursive deletion for the CLI's verified move.
- Inspect every result/error in a batch. A nonzero exit may mean partial success; do not blindly repeat successful items, which get new IDs.

The CLI verifies written bytes, parses the target and registers required native DB rows before deletion. Source cleanup targets only the selected session ID, matching transcript and known indexes. This check does not call a model.
Use the returned `resume` argv as a command suggestion; native ID lookup assumes the normal target home.
For custom target homes, launch the client with its corresponding home environment override.

## Format limits

- Native OpenCode selection: `asb convert --from opencode --session <id> --to pi --move --apply`. Default selection reads its DB; `--source-root` can point to the directory containing `opencode.db`. A manually supplied JSON export is a standalone file; cutting that export does not delete its original DB session.
- Codex / WorkBuddy / OpenCode / Antigravity native target homes must already contain client-initialized databases. Unknown schemas or required fields fail clearly. Use `--output-dir` for portable files without DB registration.
- For a Git project, start OpenCode once in the intended working directory to initialize its project index before importing there.
- WorkBuddy handles local sessions and its DB/cache. Multiple destination accounts require `--workbuddy-user-id`. Resume from WorkBuddy's session list. Blob images are omitted and counted.
- DSH reads v4 JSONL/zstd. Older logs must be upgraded by DSH first. Multiple generations block a move.
- DSH images are currently omitted and counted. Summaries become labeled user context.
- Antigravity means the agy CLI SQLite format, not direct desktop `.pb` or encrypted storage. Its home is `~/.gemini/antigravity-cli`; use `--source-root` / `--target-home` to override. Close clients using that home before native writes or cuts. Active presence locks and WAL/SHM/journal files block cuts.
- Antigravity uses a per-session DB, shared `conversation_summaries.db`, `jetbox_summaries_proto.pb`, `annotations/<id>.pbtxt` and last-conversation cache. The CLI writes and removes the selected records together. Keep portable `.db` and `.manifest.json` files together for title/cwd metadata. Summaries become labeled user context, images are omitted and counted.
- `antigravity-desktop` supports IDE 2.5.5 SQLite `.db` sessions, with source/target home `~/.gemini/antigravity-ide` (older SQLite home `~/.gemini/antigravity`). Example: `asb convert --from codex --to antigravity-desktop --project <directory> --apply`. Select a desktop ID or project as for other formats. Desktop and agy CLI convert in either direction using their distinct format names.
- Desktop native writes/cuts update both known `state.vscdb` history indexes, existing protobuf caches, title annotations and the selected conversation DB. Standard profile paths are inferred; for custom homes pass `--source-state-db` or `--target-state-db` with the corresponding globalStorage/state.vscdb. Close the desktop client first; a running language service or SQLite sidecars block modification. A same-ID legacy `.pb` blocks cuts. The shared state DB and neighboring sessions remain.
- Desktop has no returned resume command: open via its native history UI. Official language-service readback is verified by ID; GUI history discovery and model continuation are unverified. Do not claim a GUI check from file parsing or DB registration alone.
- Legacy desktop `.pb` or encrypted history must first be upgraded/imported into SQLite by the official client. Alternatively clone it through agy `/resume` → **Antigravity** and convert that CLI copy; moving the copy does not delete the desktop original.
- Cut removes selected transcript/bridge manifest, native session DB rows and known history/list entries. Shared projects, attachments, backups, auth and runtime settings are retained. A failure during source cleanup can leave a valid target and retained source: inspect errors before retrying.
- Portable `--output` / `--output-dir` files need native installation/import for ID-based resume. Pi can open any emitted Pi file via `pi --session <file>`.
- Compaction boundaries remain effective: Pi continues from the latest summary and later messages; earlier messages remain in its session history. Summaries can describe past actions; raw tool execution records are excluded.
