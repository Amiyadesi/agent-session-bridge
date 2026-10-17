"""ZCode conversation bundles and client-initialized SQLite stores."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import pi
from session_migrate.model import Event, EventKind, Provenance, Role, Session

from agent_session_bridge import stores

VERSION = "0.16.5"


class ZCodeFormat(StrEnum):
    ZCODE = "zcode"


def home() -> Path:
    base = os.environ.get("ZCODE_DATA_BASE_DIR", "").strip() or str(Path.home())
    return Path(base).expanduser() / ".zcode"


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise SessionMigrateError("ZCode record must be an object")
    return value


def _text(value) -> str:
    if not isinstance(value, str) or not value:
        raise SessionMigrateError("ZCode requires nonempty text fields")
    return value


def _stamp(value) -> str:
    if type(value) is not int or not 0 <= value <= 253402300799999:
        raise SessionMigrateError("ZCode timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def _encode(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def validate(c) -> None:
    stores.require(
        c,
        "session",
        {
            "id",
            "project_id",
            "workspace_id",
            "directory",
            "title",
            "version",
            "revert",
            "time_created",
            "time_updated",
            "title_source",
            "task_type",
        },
    )
    for table in ("message", "part"):
        stores.require(
            c,
            table,
            {
                "id",
                "session_id",
                "data",
                "time_created",
                "time_updated",
                "sequence",
            }
            | ({"message_id"} if table == "part" else set()),
        )
    stores.require(c, "session_entry", {"id", "session_id", "type", "data"})


def descriptor(root: Path) -> dict:
    db = root / "cli" / "db" / "db.sqlite"
    if not db.is_file():
        raise SessionMigrateError("ZCode DB missing; initialize ZCode in the target home first")
    with stores.connection(db) as c:
        validate(c)
    desktop = root / "v2" / "tasks-index.sqlite"
    result = {"kind": "zcode", "home": str(root.resolve()), "database": str(db.resolve())}
    if desktop.is_file():
        result["desktop_database"] = str(desktop.resolve())
    return result


def attach(c, store: dict, *, write: bool = True) -> None:
    if db := store.get("desktop_database"):
        # URI mode prevents a stale path from creating an empty desktop index.
        mode = "rw" if write else "ro"
        c.execute("ATTACH DATABASE ? AS desktop", (Path(db).as_uri() + "?mode=" + mode,))
        known = {r["name"] for r in c.execute("PRAGMA desktop.table_info(tasks)")}
        if (
            not {
                "workspace_key",
                "workspace_path",
                "task_id",
                "title",
                "provider",
                "model",
                "created_at",
                "updated_at",
                "meta_json",
                "title_overridden",
            }
            <= known
        ):
            raise SessionMigrateError("unsupported ZCode desktop tasks schema")


def paths(root: Path) -> list[Path]:
    db = Path(descriptor(root)["database"])
    with stores.connection(db) as c:
        return [db / r[0] for r in c.execute("SELECT id FROM session ORDER BY id")]


def source_bytes(path: Path, c=None) -> bytes:
    if c is None:
        with stores.connection(path.parent) as conn:
            return source_bytes(path, conn)
    validate(c)
    row = c.execute("SELECT * FROM session WHERE id=?", (path.name,)).fetchone()
    if row is None:
        raise SessionMigrateError("ZCode source session no longer exists")
    messages = []
    for message in c.execute(
        "SELECT * FROM message WHERE session_id=? "
        "ORDER BY sequence IS NULL,sequence,time_created,rowid",
        (path.name,),
    ):
        parts = [
            {
                **_object(json.loads(p["data"])),
                "id": p["id"],
                "messageID": message["id"],
                "sessionID": path.name,
            }
            for p in c.execute(
                "SELECT * FROM part WHERE session_id=? AND message_id=? "
                "ORDER BY sequence IS NULL,sequence,time_created,id",
                (path.name, message["id"]),
            )
        ]
        messages.append(
            {
                "info": {
                    **_object(json.loads(message["data"])),
                    "id": message["id"],
                    "sessionID": path.name,
                },
                "parts": parts,
            }
        )
    entries = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM session_entry WHERE session_id=? ORDER BY time_created,rowid",
            (path.name,),
        )
    ]
    return _encode(
        {
            "format": "zcode",
            "schema_version": 1,
            "info": dict(row),
            "messages": messages,
            "entries": entries,
        }
    )


def _branch(messages: list, revert: dict) -> list:
    target = revert.get("targetMessageID")
    if not target:
        return messages
    ids = [m["info"]["id"] for m in messages]
    if "keptMessageIDs" in revert:
        kept = revert["keptMessageIDs"]
        if not isinstance(kept, list) or any(
            not isinstance(mid, str) or mid not in ids for mid in kept
        ):
            raise SessionMigrateError("ZCode rewind references missing messages")
        if len(set(kept)) != len(kept):
            raise SessionMigrateError("duplicate ZCode rewind message ID")
        by_id = {m["info"]["id"]: m for m in messages}
        prefix = [by_id[mid] for mid in kept]
    elif target in ids:
        prefix = messages[: ids.index(target)]
    else:
        raise SessionMigrateError("ZCode rewind target is missing")
    cut = revert.get("branchCutAfterMessageID")
    created = revert.get("createdMessageID")
    cursor = cut or created
    if cursor and cursor not in ids:
        raise SessionMigrateError("ZCode rewind cursor is missing")
    return prefix + (messages[ids.index(cursor) + bool(cut) :] if cursor else [])


def _boundary(message: dict) -> dict | None:
    for part in message["parts"]:
        if part["type"] == "compaction" and (
            part.get("compactBoundary") or not part.get("timelineStatus")
        ):
            return _object(part.get("compactBoundary", {}))
    return None


def _active(messages: list, revert: dict) -> list:
    if revert.get("branchCutAfterMessageID"):
        messages = _branch(messages, revert)
    boundary_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if _boundary(messages[i]) is not None),
        -1,
    )
    if boundary_index >= 0:
        boundary = _boundary(messages[boundary_index])
        active = messages[boundary_index:]
        if segment := boundary.get("preservedSegment"):
            segment = _object(segment)
            ids = [m["info"]["id"] for m in messages]
            head, tail = segment.get("headMessageId"), segment.get("tailMessageId")
            if (
                head not in ids
                or tail not in ids
                or not ids.index(head) <= ids.index(tail) < boundary_index
            ):
                raise SessionMigrateError("invalid ZCode compaction preserved segment")
            retained = [
                m
                for m in messages[ids.index(head) : ids.index(tail) + 1]
                if not any(p["type"] == "compaction" for p in m["parts"])
                and not m["info"].get("error")
                and m["info"].get("semantics", {}).get("providerVisibility") != "hidden"
            ]
            anchor = segment.get("anchorMessageId")
            active_ids = [m["info"]["id"] for m in active]
            index = active_ids.index(anchor) + 1 if anchor in active_ids else 1
            active[index:index] = retained
        if not revert.get("branchCutAfterMessageID") and "keptMessageIDs" in revert:
            if not set(revert["keptMessageIDs"]) & {m["info"]["id"] for m in active}:
                return active
        messages = active
    return messages if revert.get("branchCutAfterMessageID") else _branch(messages, revert)


def _visible(info: dict, parts: list) -> bool:
    semantics = _object(info.get("semantics", {}))
    if semantics:
        if semantics.get("kind") == "timeline_event":
            return False
        if (
            semantics.get("origin") == "real_user"
            and not info.get("synthetic")
            and info.get("visibility") != "model-only"
        ):
            return True
        if (
            info["role"] == "assistant"
            and semantics.get("kind") == "assistant_response"
            and semantics.get("uiVisibility") == semantics.get("transcriptVisibility") == "visible"
        ):
            return True
        return False
    if info.get("synthetic") or info.get("visibility") == "model-only":
        return False
    metadata = [_object(info.get("metadata", {}))] + [_object(p.get("metadata", {})) for p in parts]
    if info.get("source") or any(
        m.get("source") or m.get("visibility") == "model-only" for m in metadata
    ):
        return False
    text = "".join(
        p.get("text", "") for p in parts if p["type"] == "text" and not p.get("ignored")
    ).lstrip()
    if (
        text.startswith('<system-reminder source="goal-continuation">')
        or text.startswith("<system-reminder>")
        and (
            "Continue working toward the active session goal." in text
            or "Current session goal state" in text
        )
        or any(
            t in text
            for t in [
                "Conversation rewind applied.",
                "Workspace rewind applied.",
            ]
        )
    ):
        return False
    return not any(p.get("synthetic") or p["type"] == "timeline" for p in parts)


def parse(path: Path) -> Session:
    native = path.parent.name == "db.sqlite" and path.parent.is_file()
    data = source_bytes(path) if native else path.read_bytes()
    bundle = _object(json.loads(data))
    if (
        bundle.get("format") != "zcode"
        or type(bundle.get("schema_version")) is not int
        or bundle["schema_version"] != 1
    ):
        raise SessionMigrateError("unsupported ZCode bundle format/version")
    info = _object(bundle.get("info"))
    sid = _text(info.get("id"))
    if sid in {".", ".."} or any(c in sid for c in "/\\:"):
        raise SessionMigrateError("invalid ZCode session ID")
    directory = _text(info.get("directory"))
    title = info.get("title")
    if not isinstance(title, str):
        raise SessionMigrateError("ZCode title must be text")
    messages = bundle.get("messages")
    if not isinstance(messages, list):
        raise SessionMigrateError("ZCode messages must be an array")
    message_ids, part_ids = set(), set()
    for message in messages:
        mi = _object(_object(message).get("info"))
        mid = _text(mi.get("id"))
        if mid in message_ids:
            raise SessionMigrateError("duplicate ZCode message ID")
        message_ids.add(mid)
        if mi.get("sessionID") != sid or mi.get("role") not in {"user", "assistant"}:
            raise SessionMigrateError("ZCode message identity/role mismatch")
        _stamp(_object(mi.get("time")).get("created"))
        for key in ("semantics", "metadata"):
            if key in mi:
                _object(mi[key])
        if not isinstance(message.get("parts"), list):
            raise SessionMigrateError("ZCode parts must be an array")
        for part in message["parts"]:
            _text(_object(part).get("type"))
            pid = _text(part.get("id"))
            if pid in part_ids:
                raise SessionMigrateError("duplicate ZCode part ID")
            part_ids.add(pid)
            if "metadata" in part:
                _object(part["metadata"])
            if part["type"] == "text" and not isinstance(part.get("text"), str):
                raise SessionMigrateError("ZCode text part needs text")
            if part.get("sessionID") != sid or part.get("messageID") != mi["id"]:
                raise SessionMigrateError("ZCode part identity mismatch")
    revert = info.get("revert")
    revert = _object(
        json.loads(revert) if isinstance(revert, str) else {} if revert is None else revert
    )
    for key in ("targetMessageID", "createdMessageID", "branchCutAfterMessageID"):
        if key in revert:
            _text(revert[key])
    active = _active(messages, revert)
    events = []
    for index in range(len(messages) - len(active)):
        events.append(Event(EventKind.OPAQUE, Provenance(index, "inactive_branch")))
    for index, message in enumerate(active):
        mi, parts = message["info"], message["parts"]
        provenance = Provenance(index, "zcode_message")
        timestamp = _stamp(mi["time"]["created"])
        if any(
            mi.get(key)
            for key in (
                "model",
                "modelSelection",
                "modelId",
                "modelID",
                "providerId",
                "providerID",
                "variant",
            )
        ):
            events.append(Event(EventKind.OPAQUE, provenance))
        is_summary = (mi.get("summary") is True or isinstance(mi.get("summary"), dict)) or (
            mi.get("semantics", {}).get("kind") == "compact_summary"
        )
        visible = is_summary or _visible(mi, parts)
        texts = []
        for part in parts:
            if part["type"] == "text" and not part.get("ignored") and visible:
                texts.append(part["text"])
            else:
                kind = {"tool": EventKind.TOOL_CALL, "reasoning": EventKind.THINKING}.get(
                    part["type"],
                    EventKind.OPAQUE,
                )
                events.append(Event(kind, provenance, timestamp=timestamp))
                if part["type"] == "tool" and _object(part.get("state", {})).get("status") in {
                    "completed",
                    "error",
                }:
                    events.append(Event(EventKind.TOOL_RESULT, provenance, timestamp=timestamp))
        if text := "\n".join(texts):
            events.append(
                Event(
                    EventKind.COMPACTION if is_summary else EventKind.MESSAGE,
                    provenance,
                    Role.SYSTEM if is_summary else Role(mi["role"]),
                    timestamp,
                    text,
                )
            )
    entries = bundle.get("entries", [])
    if not isinstance(entries, list):
        raise SessionMigrateError("ZCode entries must be an array")
    for index, entry in enumerate(entries):
        _object(entry)
        events.append(Event(EventKind.OPAQUE, Provenance(index, "session_entry")))
    return Session(
        source_format=ZCodeFormat.ZCODE,
        source_path=path.resolve(),
        source_sha256=hashlib.sha256(data).hexdigest(),
        session_id=sid,
        cwd=Path(directory),
        title=title or None,
        started_at=_stamp(info.get("time_created")),
        cli_version=_text(info.get("version")),
        model=None,
        events=tuple(events),
        raw_record_count=len(messages) + len(bundle.get("entries", [])),
    )


def serialize(session: Session, *, session_id: str, cwd: Path) -> tuple[bytes, dict]:
    data, losses = pi.serialize(session, session_id=session_id, cwd=cwd)
    dropped = Counter(losses)
    rows = list(map(json.loads, data.splitlines()))
    messages, parent = [], None
    for row in rows[1:]:
        summary = row["type"] == "compaction"
        if not summary and row["type"] != "message":
            continue
        if summary:
            role, text = "user", row["summary"]
        else:
            role, content = row["message"]["role"], row["message"]["content"]
            if role not in {"user", "assistant"}:
                dropped["zcode:non_dialogue"] += 1
                continue
            if isinstance(content, str):
                text = content
            else:
                text = "\n".join(b["text"] for b in content if b["type"] == "text")
                for block in content:
                    if block["type"] != "text":
                        dropped[f"zcode:{block['type']}"] += 1
        if not text:
            continue
        stamp = stores._ms(row["timestamp"])
        mid = "msg_" + uuid.uuid4().hex
        info = {
            "id": mid,
            "sessionID": session_id,
            "role": role,
            "time": {"created": stamp},
            "agent": "build",
        }
        if role == "user":
            info["model"] = {}
            parent = mid
        else:
            if parent is None:
                raise SessionMigrateError("ZCode assistant needs a preceding user message")
            info.update(
                {
                    "parentID": parent,
                    "mode": "build",
                    "path": {"cwd": str(cwd), "root": str(cwd)},
                    "cost": 0,
                    "finish": "stop",
                    "tokens": {
                        "input": 0,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            )
            info["time"]["completed"] = stamp
        parts = []
        if summary:
            info.update(
                {
                    "synthetic": True,
                    "visibility": "model-only",
                    "semantics": {
                        "origin": "migration",
                        "kind": "compact_summary",
                        "uiVisibility": "hidden",
                        "providerVisibility": "visible",
                        "transcriptVisibility": "hidden",
                    },
                }
            )
            parts.append({"type": "compaction", "auto": False})
        parts.append({"type": "text", "text": text})
        for part in parts:
            part.update(
                {"id": "part_" + uuid.uuid4().hex, "sessionID": session_id, "messageID": mid}
            )
        messages.append({"info": info, "parts": parts})
    if not messages:
        raise SessionMigrateError("ZCode export needs conversation text")
    created = stores._ms(rows[0]["timestamp"])
    return _encode(
        {
            "format": "zcode",
            "schema_version": 1,
            "info": {
                "id": session_id,
                "directory": str(cwd),
                "title": session.title or "Imported conversation",
                "version": VERSION,
                "time_created": created,
                "time_updated": created,
            },
            "messages": messages,
        }
    ), dict(sorted(dropped.items()))


def register(store: dict, target: dict, payload: bytes) -> None:
    bundle = json.loads(payload)
    info = bundle["info"]
    sid, cwd = info["id"], info["directory"]
    project = re.sub(r"[^a-z0-9._-]+", "-", cwd.lower()).strip("-")[:80] or "default"
    with stores.connection(Path(store["database"]), write=True) as c:
        validate(c)
        attach(c, store)
        stores.insert(
            c,
            "session",
            {
                **info,
                "project_id": "proj_" + project,
                "workspace_id": None,
                "path": cwd,
                "slug": sid,
                "title_source": "custom",
                "task_type": "interactive",
                "time_title_updated": info["time_updated"],
            },
        )
        for sequence, message in enumerate(bundle["messages"]):
            mi = message["info"]
            stamp = mi["time"]["created"]
            stores.insert(
                c,
                "message",
                {
                    "id": mi["id"],
                    "session_id": sid,
                    "time_created": stamp,
                    "time_updated": stamp,
                    "sequence": sequence,
                    "data": json.dumps(
                        {k: v for k, v in mi.items() if k not in {"id", "sessionID"}}
                    ),
                },
            )
            for order, part in enumerate(message["parts"]):
                stores.insert(
                    c,
                    "part",
                    {
                        "id": part["id"],
                        "session_id": sid,
                        "message_id": mi["id"],
                        "time_created": stamp,
                        "time_updated": stamp,
                        "sequence": order,
                        "data": json.dumps(
                            {
                                k: v
                                for k, v in part.items()
                                if k not in {"id", "sessionID", "messageID"}
                            }
                        ),
                    },
                )
        if store.get("desktop_database"):
            meta = {
                "taskId": sid,
                "traceId": "zcode-" + sid,
                "title": info["title"],
                "titleOverridden": True,
                "workspacePath": cwd,
                "createdAt": info["time_created"],
                "updatedAt": info["time_updated"],
                "mode": "default",
                "provider": "glm",
                "status": "completed",
            }
            c.execute(
                "INSERT INTO desktop.tasks (workspace_key,workspace_path,task_id,title,provider,"
                "mode,created_at,updated_at,title_overridden,task_status,meta_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cwd,
                    cwd,
                    sid,
                    info["title"],
                    "glm",
                    "default",
                    info["time_created"],
                    info["time_updated"],
                    1,
                    "completed",
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
        readback = json.loads(source_bytes(Path(store["database"]) / sid, c))
        if readback["messages"] != bundle["messages"] or readback["info"]["title"] != info["title"]:
            raise SessionMigrateError("ZCode native registration read-back failed")


def check_cut(c, store: dict, sid: str) -> None:
    validate(c)
    if not c.execute("SELECT 1 FROM session WHERE id=?", (sid,)).fetchone():
        raise SessionMigrateError("ZCode source no longer exists")
    # Workflow/automation ownership spans other sessions; leave these intact for the client.
    references = [
        ("session", "parent_id"),
        ("dwf_run", "parent_session_id"),
        ("dwf_actor", "session_id"),
        ("workflow_run", "parent_session_id"),
        ("workflow_activity", "child_session_id"),
        ("session_task_link", "parent_session_id"),
        ("session_task_link", "child_session_id"),
    ]
    for table, key in references:
        if (
            key in stores.columns(c, table)
            and c.execute(
                f"SELECT 1 FROM {stores.quote(table)} WHERE {stores.quote(key)}=? LIMIT 1",
                (sid,),
            ).fetchone()
        ):
            raise SessionMigrateError("ZCode cut refuses linked child/workflow sessions; use copy")
    if (
        "status" in stores.columns(c, "session_input")
        and c.execute(
            "SELECT 1 FROM session_input WHERE session_id=? "
            "AND status NOT IN ('promoted','cancelled','discarded','failed') LIMIT 1",
            (sid,),
        ).fetchone()
    ):
        raise SessionMigrateError("ZCode cut refuses pending inputs; stop the source session first")
    for table in ("model_usage", "tool_usage", "turn_usage"):
        if (
            "status" in stores.columns(c, table)
            and c.execute(
                f"SELECT 1 FROM {table} WHERE session_id=? AND status='running' LIMIT 1",
                (sid,),
            ).fetchone()
        ):
            raise SessionMigrateError(
                "ZCode cut refuses running usage; stop the source session first"
            )
    if store.get("desktop_database"):
        for table, key in [
            ("automations", "target_task_id"),
            ("automation_runs", "session_id"),
            ("off_peak_tasks", "session_id"),
            ("off_peak_tasks", "conversation_id"),
        ]:
            cols = {r["name"] for r in c.execute(f"PRAGMA desktop.table_info({table})")}
            if (
                key in cols
                and c.execute(
                    f"SELECT 1 FROM desktop.{table} WHERE {key}=? LIMIT 1",
                    (sid,),
                ).fetchone()
            ):
                raise SessionMigrateError("ZCode cut refuses automation sessions; use copy")


def delete(c, store: dict, sid: str) -> None:
    check_cut(c, store, sid)
    if store.get("desktop_database"):
        rows = c.execute(
            "SELECT workspace_key FROM desktop.tasks WHERE task_id=?",
            (sid,),
        ).fetchall()
        for row in rows:
            for table, sql, args in [
                ("task_group_members", "workspace_key=? AND task_id=?", (row[0], sid)),
                (
                    "task_group_view_node_orders",
                    "node_type='task' AND node_key=?",
                    (json.dumps([row[0], sid], separators=(",", ":"), ensure_ascii=False),),
                ),
            ]:
                if c.execute(f"PRAGMA desktop.table_info({table})").fetchone():
                    c.execute(f"DELETE FROM desktop.{table} WHERE {sql}", args)
        c.execute("DELETE FROM desktop.tasks WHERE task_id=?", (sid,))
    for table in (
        "part",
        "message",
        "session_entry",
        "input_history",
        "session_input",
        "todo",
        "session_target",
        "model_usage",
        "tool_usage",
        "turn_usage",
    ):
        if "session_id" in stores.columns(c, table):
            c.execute(f"DELETE FROM {stores.quote(table)} WHERE session_id=?", (sid,))
    if {"scope", "scope_id"} <= stores.columns(c, "local_setting"):
        c.execute("DELETE FROM local_setting WHERE scope='session' AND scope_id=?", (sid,))
    c.execute("DELETE FROM session WHERE id=?", (sid,))
