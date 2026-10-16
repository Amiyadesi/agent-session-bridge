"""Native SQLite indexes and narrowly scoped session deletion.

Clients own schema initialization. Unknown required columns fail before a source is removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def same_path(left: Path, right: Path) -> bool:
    def normal(path):
        text = str(path.resolve())
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
        return os.path.normcase(os.path.normpath(text))

    return normal(left) == normal(right)


@contextmanager
def connection(path: Path, *, write: bool = False):
    # mode=rw/ro prevents an incorrect path from creating an empty database.
    conn = sqlite3.connect(
        path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=3
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        if write:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({quote(table)})")}


def require(conn, table: str, names: set[str]) -> None:
    if not names <= columns(conn, table):
        raise SessionMigrateError(f"unsupported native database schema: {table}")


def insert(conn, table: str, values: dict) -> None:
    known = columns(conn, table)
    values = {k: v for k, v in values.items() if k in known}
    # Do not invent defaults for newly required fields.
    required = {
        r["name"]
        for r in conn.execute(f"PRAGMA table_info({quote(table)})")
        if r["notnull"] and r["dflt_value"] is None and not r["pk"]
    }
    if required - values.keys():
        raise SessionMigrateError(
            f"unsupported required columns in {table}: "
            + ", ".join(sorted(required - values.keys()))
        )
    conn.execute(
        f"INSERT INTO {quote(table)} ({','.join(map(quote, values))}) "
        f"VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def database(kind: str, home: Path) -> Path:
    if kind == "codex":
        candidates = [
            (int(m[1]), p)
            for p in home.glob("state_*.sqlite")
            if (m := re.fullmatch(r"state_(\d+)\.sqlite", p.name))
        ]
        if candidates:
            return max(candidates)[1]
        raise SessionMigrateError("Codex state DB missing; start Codex once in the target home")
    path = home / ("workbuddy.db" if kind == "workbuddy" else "opencode.db")
    if not path.is_file():
        raise SessionMigrateError(f"{kind} DB missing; initialize the client in the target home")
    return path


def descriptor(kind: str, home: Path, *, user_id: str | None = None) -> dict:
    db = database(kind, home)
    table = {"codex": "threads", "workbuddy": "sessions", "opencode": "session"}[kind]
    with connection(db) as c:
        require(c, table, {"id", "cwd"} if kind != "opencode" else {"id", "directory"})
        if kind == "opencode":
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='session_v2'").fetchone():
                raise SessionMigrateError("unsupported OpenCode session_v2 schema")
            require(c, "message", {"id", "session_id", "data"})
            require(c, "part", {"id", "session_id", "message_id", "data"})
        if kind == "workbuddy" and user_id is None:
            users = [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT user_id FROM sessions "
                    "WHERE user_id != '' AND user_id IS NOT NULL"
                )
            ]
            if len(users) > 1:
                raise SessionMigrateError("multiple WorkBuddy accounts; pass --workbuddy-user-id")
            user_id = users[0] if users else ""
    return {
        "kind": kind,
        "home": str(home.resolve()),
        "database": str(db.resolve()),
        **({"user_id": user_id} if kind == "workbuddy" else {}),
    }


def source_store(session) -> dict | None:
    kind, path = session.source_format.value, session.source_path
    if kind == "opencode" and path.parent.name == "opencode.db" and path.parent.is_file():
        return descriptor(kind, path.parent.parent)
    if kind not in {"codex", "workbuddy"}:
        return None
    markers = {"codex": {"sessions", "archived_sessions"}, "workbuddy": {"projects"}}[kind]
    for parent in path.parents:
        if parent.name in markers:
            home = parent.parent
            # A standalone export directory may happen to be named 'sessions'.
            if kind == "codex" and not list(home.glob("state_*.sqlite")):
                return None
            if kind == "workbuddy" and not (home / "workbuddy.db").exists():
                raise SessionMigrateError(
                    "WorkBuddy native source has no DB; refusing incomplete cut"
                )
            return descriptor(kind, home, user_id="" if kind == "workbuddy" else None)
    return None


def _ms(stamp: str | None) -> int:
    return (
        int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
        if stamp
        else int(time.time() * 1000)
    )


def register(store: dict, manifest: dict, payload: bytes, source) -> None:
    target, kind = manifest["target"], store["kind"]
    sid, cwd = target["session_id"], target["cwd"]
    now, created = int(time.time() * 1000), _ms(source.started_at)
    title = source.title or "Imported conversation"
    with connection(Path(store["database"]), write=True) as c:
        if kind == "codex":
            meta = json.loads(payload.splitlines()[0])["payload"]
            insert(
                c,
                "threads",
                {
                    "id": sid,
                    "rollout_path": target["path"],
                    "cwd": cwd,
                    "title": title,
                    "name": title,
                    "source": "cli",
                    "model_provider": meta.get("model_provider", "openai"),
                    "sandbox_policy": '{"type":"workspace-write"}',
                    "approval_mode": "on-request",
                    "created_at": created // 1000,
                    "updated_at": now // 1000,
                    "created_at_ms": created,
                    "updated_at_ms": now,
                    "recency_at": now // 1000,
                    "recency_at_ms": now,
                    "has_user_event": 1,
                    "cli_version": meta.get("cli_version", ""),
                    "history_mode": "legacy",
                    "first_user_message": "",
                    "preview": "",
                },
            )
        elif kind == "workbuddy":
            cwd = cwd.replace("\\", "/")
            insert(
                c,
                "sessions",
                {
                    "id": sid,
                    "cwd": cwd,
                    "user_id": store["user_id"],
                    "title": title,
                    "status": "active",
                    "created_at": created,
                    "updated_at": now,
                    "last_activity_at": now,
                    "is_playground": 0,
                    "deleted_at": None,
                    "transport": "local",
                    "mode": "code",
                    "permission_mode": "default",
                },
            )
            require(c, "workspaces", {"path", "last_opened_at"})
            c.execute(
                "INSERT INTO workspaces(path,last_opened_at) VALUES (?,?) "
                "ON CONFLICT(path) DO NOTHING",
                (cwd, now),
            )
        else:
            bundle = json.loads(payload)
            info = bundle["info"]
            # Reuse an initialized native project when available. 'global' is the
            # client's canonical project for a directory outside a git repository.
            projects = [
                r
                for r in c.execute("SELECT id,worktree FROM project")
                if r["id"] != "global"
                and Path(cwd).resolve().is_relative_to(Path(r["worktree"]).resolve())
            ]
            project = max(projects, key=lambda r: len(r["worktree"])) if projects else None
            if project is None and any(
                (p / ".git").exists() for p in (Path(cwd), *Path(cwd).parents)
            ):
                raise SessionMigrateError(
                    "OpenCode git project is not indexed; "
                    "start OpenCode in the target directory once"
                )
            project_id = project["id"] if project else "global"
            if not c.execute("SELECT 1 FROM project WHERE id=?", (project_id,)).fetchone():
                insert(
                    c,
                    "project",
                    {
                        "id": project_id,
                        "worktree": "/",
                        "sandboxes": "[]",
                        "time_created": now,
                        "time_updated": now,
                    },
                )
            insert(
                c,
                "session",
                {
                    "id": sid,
                    "project_id": project_id,
                    "slug": info["slug"],
                    "directory": cwd,
                    "title": info["title"],
                    "version": info["version"],
                    "time_created": info["time"]["created"],
                    "time_updated": now,
                },
            )
            for msg in bundle["messages"]:
                mid = msg["info"]["id"]
                data = {k: v for k, v in msg["info"].items() if k not in {"id", "sessionID"}}
                insert(
                    c,
                    "message",
                    {
                        "id": mid,
                        "session_id": sid,
                        "time_created": msg["info"]["time"]["created"],
                        "time_updated": now,
                        "data": json.dumps(data),
                    },
                )
                for part in msg["parts"]:
                    data = {
                        k: v for k, v in part.items() if k not in {"id", "sessionID", "messageID"}
                    }
                    insert(
                        c,
                        "part",
                        {
                            "id": part["id"],
                            "message_id": mid,
                            "session_id": sid,
                            "time_created": now,
                            "time_updated": now,
                            "data": json.dumps(data),
                        },
                    )
        table = {"codex": "threads", "workbuddy": "sessions", "opencode": "session"}[kind]
        if not c.execute(f"SELECT 1 FROM {table} WHERE id=?", (sid,)).fetchone():
            raise SessionMigrateError("native index registration read-back failed")


def opencode_paths(home: Path) -> list[Path]:
    db = Path(descriptor("opencode", home)["database"])
    with connection(db) as c:
        require(c, "session", {"id", "directory"})
        return [db / r[0] for r in c.execute("SELECT id FROM session ORDER BY id")]


def opencode_bytes(path: Path, conn=None) -> bytes:
    if conn is None:
        with connection(path.parent) as c:
            return opencode_bytes(path, c)
    c = conn
    require(c, "session", {"id", "directory", "time_created", "time_updated"})
    r = c.execute("SELECT * FROM session WHERE id=?", (path.name,)).fetchone()
    if not r:
        raise SessionMigrateError("OpenCode source session no longer exists")
    info = {
        "id": r["id"],
        "slug": r["slug"],
        "projectID": r["project_id"],
        "directory": r["directory"],
        "title": r["title"],
        "version": r["version"],
        "time": {"created": r["time_created"], "updated": r["time_updated"]},
    }
    messages = []
    for m in c.execute("SELECT * FROM message WHERE session_id=? ORDER BY id", (r["id"],)):
        mi = {**json.loads(m["data"]), "id": m["id"], "sessionID": r["id"]}
        parts = [
            {**json.loads(p["data"]), "id": p["id"], "messageID": m["id"], "sessionID": r["id"]}
            for p in c.execute("SELECT * FROM part WHERE message_id=? ORDER BY id", (m["id"],))
        ]
        messages.append({"info": mi, "parts": parts})
    return json.dumps(
        {"info": info, "messages": messages}, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")


def load_opencode(path: Path):
    data = opencode_bytes(path)
    with tempfile.TemporaryDirectory(prefix="asb-read-") as root:
        temporary = Path(root) / "export.json"
        temporary.write_bytes(data)
        session = opencode.parse_session(temporary)
    return replace(
        session, source_path=path.resolve(), source_sha256=hashlib.sha256(data).hexdigest()
    )


def _delete_rows(c, store: dict, sid: str, path: Path | None = None) -> None:
    kind = store["kind"]
    table = {"codex": "threads", "workbuddy": "sessions", "opencode": "session"}[kind]
    row = c.execute(f"SELECT * FROM {table} WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise SessionMigrateError("source native index row missing; source retained")
    if kind == "codex" and path is not None:
        if not same_path(Path(row["rollout_path"]), path):
            raise SessionMigrateError("Codex DB path does not match selected transcript")
    if kind == "workbuddy" and path is not None:
        from agent_session_bridge.workbuddy import project_key

        expected = Path(store["home"]) / "projects" / project_key(Path(row["cwd"])) / f"{sid}.jsonl"
        if not same_path(expected, path):
            raise SessionMigrateError("WorkBuddy DB path does not match selected transcript")
    if kind == "workbuddy" and "transport" in row.keys() and row["transport"] != "local":
        raise SessionMigrateError("cloud WorkBuddy sessions cannot be cut locally")
    # Explicit non-FK references in the supported schemas. FK children cascade.
    references = {
        "codex": [
            ("thread_spawn_edges", "parent_thread_id"),
            ("thread_spawn_edges", "child_thread_id"),
        ],
        "workbuddy": [
            ("session_usage", "session_id"),
            ("automation_runs", "thread_id"),
            ("automation_delivery_outbox", "run_id"),
        ],
        "opencode": [("part", "session_id"), ("message", "session_id")],
    }[kind]
    # Some released schemas omit FKs; delete direct session children explicitly.
    for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        name = r[0]
        for fk in c.execute(f"PRAGMA foreign_key_list({quote(name)})"):
            if fk["table"] == table and fk["to"] == "id":
                references.append((name, fk["from"]))
    for name, key in set(references):
        if key in columns(c, name):
            c.execute(f"DELETE FROM {quote(name)} WHERE {quote(key)}=?", (sid,))
    c.execute(f"DELETE FROM {table} WHERE id=?", (sid,))


def _caches(session, store: dict | None) -> list[tuple[Path, bytes, bytes]]:
    changes = []
    sid, kind = session.session_id, session.source_format.value
    home = Path(store["home"]) if store else None
    paths = []
    if kind == "codex" and home:
        paths = [(home / "session_index.jsonl", "id"), (home / "history.jsonl", "session_id")]
    elif kind == "workbuddy" and home:
        p = home / "app" / "sessions.json"
        if p.exists():
            original = p.read_bytes()
            value = json.loads(original)
            value["sessions"] = [r for r in value["sessions"] if r["conversationId"] != sid]
            changes.append((p, original, (json.dumps(value, ensure_ascii=False) + "\n").encode()))
    elif kind == "claude":
        for parent in session.source_path.parents:
            if parent.name == "projects":
                paths.append((parent.parent / "history.jsonl", "sessionId"))
                break
        p = session.source_path.parent / "sessions-index.json"
        if p.exists():
            original = p.read_bytes()
            value = json.loads(original)
            value["entries"] = [r for r in value["entries"] if r["sessionId"] != sid]
            changes.append((p, original, (json.dumps(value, ensure_ascii=False) + "\n").encode()))
    for p, key in paths:
        if p.exists():
            original = p.read_bytes()
            updated = b"".join(
                line
                for line in original.splitlines(keepends=True)
                if line.strip() and json.loads(line).get(key) != sid
            )
            if updated != original:
                changes.append((p, original, updated))
    return changes


def _replace(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".asb-index-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def remove_source(session) -> None:
    store = source_store(session)
    virtual = session.source_format.value == "opencode" and store is not None
    changes = _caches(session, store)
    renamed = []
    written = []
    try:
        with connection(Path(store["database"]), write=True) if store else _no_database() as c:
            data = (
                opencode_bytes(session.source_path, c)
                if virtual
                else session.source_path.read_bytes()
            )
            if hashlib.sha256(data).hexdigest() != session.source_sha256:
                raise SessionMigrateError(
                    "source changed during conversion; target kept, source retained"
                )
            if store:
                _delete_rows(
                    c, store, session.session_id, session.source_path if not virtual else None
                )
            if not virtual:
                paths = [session.source_path]
                if session.source_format.value == "workbuddy":
                    paths.extend(
                        p
                        for suffix in (".meta.json", ".file-rollback.ndjson")
                        if (p := session.source_path.with_suffix(suffix)).is_file()
                    )
            else:
                bundle = Path(store["home"]) / "asb-imports" / f"{session.session_id}.json"
                sidecar = bundle.with_name(bundle.name + ".manifest.json")
                if bundle.exists() and sidecar.is_file():
                    value = json.loads(sidecar.read_bytes())
                    target = value.get("target", {})
                    if (
                        target.get("session_id") != session.session_id
                        or target.get("sha256") != hashlib.sha256(bundle.read_bytes()).hexdigest()
                    ):
                        raise SessionMigrateError("OpenCode bridge export changed; source retained")
                    paths = [bundle]
                else:
                    paths = []
            for path in list(paths):
                sidecar = path.with_name(path.name + ".manifest.json")
                if sidecar.exists():
                    value = json.loads(sidecar.read_bytes())
                    if value.get("target", {}).get("session_id") == session.session_id:
                        paths.append(sidecar)
            for path in paths:
                temporary = path.with_name(".asb-cut-" + uuid.uuid4().hex)
                path.rename(temporary)
                renamed.append((path, temporary))
            for path, original, updated in changes:
                if path.read_bytes() != original:
                    raise SessionMigrateError(
                        "source index changed; retry after stopping source client"
                    )
                _replace(path, updated)
                written.append((path, original, updated))
    except BaseException:
        for path, original, updated in reversed(written):
            if path.read_bytes() == updated:
                _replace(path, original)
        for path, temporary in reversed(renamed):
            if not path.exists():
                temporary.rename(path)
        raise
    for _, temporary in renamed:
        temporary.unlink()


@contextmanager
def _no_database():
    yield None
