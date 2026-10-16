"""Experimental WorkBuddy transcript files; native database registration is separate."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import pi
from session_migrate.formats.common import valid_rfc3339
from session_migrate.jsonl import encode_jsonl, file_sha256, iter_jsonl
from session_migrate.model import Event, EventKind, Provenance, Role, Session


class WorkBuddyFormat(StrEnum):
    WORKBUDDY = "workbuddy"


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is int and 0 <= value <= 253402300799999:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and valid_rfc3339(value):
        return value
    raise SessionMigrateError("WorkBuddy timestamp must be epoch milliseconds or RFC3339")


def parse(path: Path) -> Session:
    records = list(iter_jsonl(path))
    identifiers = {
        r.value["sessionId"] for r in records if isinstance(r.value.get("sessionId"), str)
    }
    if len(identifiers) != 1 or not next(iter(identifiers)):
        raise SessionMigrateError("WorkBuddy transcript needs one nonempty sessionId")
    events = []
    cwd, started_at, title = None, None, None
    for record in records:
        row = record.value
        if row.get("sessionId") is not None and not isinstance(row["sessionId"], str):
            raise SessionMigrateError("WorkBuddy sessionId must be text")
        timestamp = _timestamp(row.get("timestamp"))
        started_at = started_at or timestamp
        if isinstance(row.get("cwd"), str) and row["cwd"]:
            cwd = cwd or Path(row["cwd"])
        kind = row.get("type")
        if not isinstance(kind, str):
            raise SessionMigrateError("WorkBuddy record type must be text")
        provenance = Provenance(record.index, kind if isinstance(kind, str) else None)
        if kind in {"ai-title", "custom-title"}:
            value = row.get("customTitle") or row.get("aiTitle")
            if isinstance(value, str) and value:
                title = value
            continue
        if kind != "message":
            event_kind = {
                "function_call": EventKind.TOOL_CALL,
                "function_call_result": EventKind.TOOL_RESULT,
                "reasoning": EventKind.THINKING,
            }.get(kind, EventKind.OPAQUE)
            events.append(Event(event_kind, provenance, timestamp=timestamp))
            continue
        try:
            role = Role(row.get("role"))
        except ValueError as exc:
            raise SessionMigrateError("WorkBuddy message has an unsupported role") from exc
        content = row.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            raise SessionMigrateError("WorkBuddy message content must be text or an array")
        texts = []
        for block in content:
            if not isinstance(block, dict):
                raise SessionMigrateError("WorkBuddy content block must be an object")
            block_kind = block.get("type")
            if not isinstance(block_kind, str):
                raise SessionMigrateError("WorkBuddy content block type must be text")
            if block_kind in {"text", "input_text", "output_text"}:
                if not isinstance(block.get("text"), str):
                    raise SessionMigrateError("WorkBuddy text block needs text")
                texts.append(block["text"])
            else:
                event_kind = {
                    "tool_use": EventKind.TOOL_CALL,
                    "tool_result": EventKind.TOOL_RESULT,
                    "thinking": EventKind.THINKING,
                }.get(block_kind, EventKind.OPAQUE)
                # Blob paths are untrusted references; never open arbitrary local files.
                events.append(Event(event_kind, provenance, role, timestamp))
        text = "\n".join(texts)
        if role == Role.USER and text.lstrip().startswith("<cb_summary>"):
            text = text.strip().removeprefix("<cb_summary>").removesuffix("</cb_summary>").strip()
            events.append(Event(EventKind.COMPACTION, provenance, Role.SYSTEM, timestamp, text))
        elif text:
            if role == Role.USER:
                queries = re.findall(r"<user_query>(.*?)</user_query>", text, flags=re.S)
                if queries:
                    text = queries[-1].strip()
            events.append(Event(EventKind.MESSAGE, provenance, role, timestamp, text))
    return Session(
        source_format=WorkBuddyFormat.WORKBUDDY,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=next(iter(identifiers)),
        cwd=cwd,
        started_at=started_at,
        cli_version=None,
        model=None,
        title=title,
        events=tuple(events),
        raw_record_count=len(records),
    )


def serialize(session: Session, *, session_id: str, cwd: Path) -> tuple[bytes, dict[str, int]]:
    payload, losses = pi.serialize(session, session_id=session_id, cwd=cwd)
    dropped = Counter(losses)
    rows = []
    for row in map(json.loads, payload.splitlines()[1:]):
        if row["type"] == "compaction":
            role, text = "user", "<cb_summary>\n" + row["summary"] + "\n</cb_summary>"
        elif row["type"] == "message":
            message = row["message"]
            if message["role"] not in {"user", "assistant"}:
                dropped["workbuddy:non_dialogue"] += 1
                continue
            role = message["role"]
            content = message["content"]
            if isinstance(content, str):
                text = content
            else:
                text = "\n".join(block["text"] for block in content if block["type"] == "text")
                for block in content:
                    if block["type"] != "text":
                        dropped[f"workbuddy:{block['type']}"] += 1
        else:
            continue
        if text:
            rows.append(
                {
                    "type": "message",
                    "id": str(uuid.uuid4()),
                    "parentId": rows[-1]["id"] if rows else None,
                    "role": role,
                    "sessionId": session_id,
                    "cwd": str(cwd),
                    "timestamp": row["timestamp"],
                    "content": [
                        {"type": "input_text" if role == "user" else "output_text", "text": text}
                    ],
                }
            )
    if not rows:
        raise SessionMigrateError("WorkBuddy export needs conversation text")
    if session.title:
        rows.append({"type": "ai-title", "sessionId": session_id, "aiTitle": session.title})
    return encode_jsonl(rows), dict(sorted(dropped.items()))


def project_key(cwd: Path) -> str:
    key = re.sub(r"-+", "-", re.sub(r"[/\\:]", "-", str(cwd))).strip("-")
    if len(key.encode("utf-8")) > 255:
        raise SessionMigrateError("WorkBuddy project path is too long for this adapter")
    return key
