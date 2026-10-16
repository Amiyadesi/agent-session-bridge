"""DSH format-v4 JSONL codec. Never changes the original session store."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from compression import zstd
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import pi
from session_migrate.model import Event, EventKind, Provenance, Role, Session

VERSION = 4
MAX_BYTES = 256 * 1024 * 1024
SURFACE_TYPES = {
    "system/message",
    "developer/message",
    "user/message",
    "assistant/message",
    "tool/result",
}
LOG_TYPES = {
    "agent-preset/selected",
    "approval/asked",
    "approval/decided",
    "approval/policy",
    "command/done",
    "command/run",
    "deliverables/presented",
    "feedback/message-delete",
    "feedback/message-put",
    "feedback/record",
    "goal/change",
    "hook/invoked",
    "hook/result",
    "job/registered",
    "job/settled",
    "model/selection",
    "permission/preset",
    "plan/mode",
    "request/context",
    "sandbox/mode",
    "schedule/change",
    "session-log-deepseek/delivery-accepted",
    "subagent/descriptor",
    "subagent/model-selection-policy",
    "team/member",
    "team/message/delivered",
    "team/message/queued",
    "team/task",
    "todo/write",
    "tool-workflow/agent-end",
    "tool-workflow/agent-start",
    "tool-workflow/run-end",
    "tool-workflow/run-start",
    "tool/ptc-dispatch",
    "tool/ptc-dispatch-start",
    "web/deepseek-search-llm-request",
    "working-directory/change",
    "workspace/changes",
    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "tool/call",
    "assistant/attempt",
    "request/header",
    "session/title",
    "session/end-seed",
    "compaction/start",
    "compaction/end",
    "compaction/summary",
    "compaction/prune",
    "llm/retry",
    "llm/retry-started",
    "subagent/catalog",
    "session/title-llm-request",
}


class DshFormat(StrEnum):
    DSH = "dsh"


def _object(value: Any, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionMigrateError(f"DSH {subject} must be an object")
    return value


def _text(value: Any, subject: str) -> str:
    if not isinstance(value, str) or not value:
        raise SessionMigrateError(f"DSH {subject} must be a nonempty string")
    return value


def _ms(value: Any) -> str:
    if type(value) is not int or not 0 <= value <= 253402300799999:
        raise SessionMigrateError("DSH time must be a nonnegative Unix millisecond integer")
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def _reject_constant(value: str) -> None:
    raise SessionMigrateError(f"invalid JSON constant: {value}")


def _source_seqs(value: Any, event_seq: int) -> set[int]:
    if not isinstance(value, list):
        raise SessionMigrateError("DSH sourceEventSeqs must be an array")
    result: list[int] = []
    has_range = False
    for entry in value:
        if isinstance(entry, list):
            if len(entry) != 2 or any(type(item) is not int for item in entry):
                raise SessionMigrateError("invalid DSH sourceEventSeqs range")
            start, end = entry
            if not 0 <= start <= end < event_seq or end - start + 1 > event_seq - len(result):
                raise SessionMigrateError("DSH sourceEventSeqs range exceeds its event")
            result.extend(range(start, end + 1))
            has_range = True
        elif type(entry) is int and 0 <= entry < event_seq:
            result.append(entry)
        else:
            raise SessionMigrateError("DSH sourceEventSeqs must reference earlier events")
    if len(set(result)) != len(result) or (has_range and result != sorted(result)):
        raise SessionMigrateError("invalid DSH sourceEventSeqs ordering or duplicates")
    return set(result)


def read_records(path: Path) -> tuple[list[dict[str, Any]], str]:
    if path.stat().st_size > MAX_BYTES:
        raise SessionMigrateError("DSH input exceeds 256 MiB")
    raw = path.read_bytes()
    if path.suffix == ".zstd":
        decoder = zstd.ZstdDecompressor()
        # Independent frames are how DSH appends batches. Bound expanded data too.
        remaining = raw
        chunks: list[bytes] = []
        size = 0
        while remaining:
            chunk = decoder.decompress(remaining, max_length=MAX_BYTES + 1 - size)
            size += len(chunk)
            if size > MAX_BYTES:
                raise SessionMigrateError("expanded DSH input exceeds 256 MiB")
            chunks.append(chunk)
            while not decoder.needs_input and not decoder.eof:
                chunk = decoder.decompress(b"", max_length=MAX_BYTES + 1 - size)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise SessionMigrateError("expanded DSH input exceeds 256 MiB")
                chunks.append(chunk)
            if not decoder.eof:
                raise SessionMigrateError("truncated DSH zstd frame; recover it with DSH first")
            remaining = decoder.unused_data
            decoder = zstd.ZstdDecompressor()
        data = b"".join(chunks)
    else:
        data = raw
    if not data.endswith(b"\n"):
        raise SessionMigrateError("DSH log has an incomplete final line; recover it with DSH first")
    records = []
    for number, line in enumerate(data.decode("utf-8-sig").splitlines(), start=1):
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise SessionMigrateError(f"invalid DSH JSON at line {number}") from exc
        records.append(_object(row, f"line {number}"))
    if not records:
        raise SessionMigrateError("empty DSH log")
    return records, hashlib.sha256(raw).hexdigest()


def parse(path: Path) -> Session:
    records, digest = read_records(path)
    header = records[0]
    if header.get("type") != "session" or header.get("version") != VERSION:
        raise SessionMigrateError(
            "supported DSH format: v4; open older logs in DSH to upgrade first"
        )
    session_id = _text(header.get("id"), "session id")
    started_at = _ms(header.get("createdAt"))
    if (
        type(header.get("isSeeded")) is not bool
        or type(header.get("delegationDepth")) is not int
        or header["delegationDepth"] < 0
    ):
        raise SessionMigrateError("DSH v4 header needs isSeeded and delegationDepth")
    cwd = header.get("cwd")
    if cwd is not None:
        _text(cwd, "cwd")
    surface: list[dict[str, Any]] = []
    opaque: list[Event] = []
    title = model = provider = None
    for index, row in enumerate(records[1:]):
        if type(row.get("seq")) is not int or row["seq"] != index:
            raise SessionMigrateError("DSH seq must be contiguous from zero")
        timestamp = _ms(row.get("time"))
        kind = _text(row.get("type"), "event type")
        data = _object(row.get("data"), "event data")
        if kind not in SURFACE_TYPES | LOG_TYPES and row.get("ignorable") is not True:
            raise SessionMigrateError(f"unsupported required DSH event: {kind}")
        if kind == "session/title":
            title = _text(data.get("title"), "title")
        if kind not in SURFACE_TYPES:
            if kind not in {"turn/start", "turn/end", "step/start", "step/end", "tool/call"}:
                opaque.append(
                    Event(
                        EventKind.OPAQUE,
                        Provenance(index + 1, kind),
                        timestamp=timestamp,
                        payload={"reason": f"dsh:{kind}"},
                    )
                )
            continue
        operation = row.get("surfaceOp")
        if operation == "append":
            surface.append(row)
        elif isinstance(operation, dict) and operation.get("op") == "replace":
            seqs = [item["seq"] for item in surface]
            try:
                first = seqs.index(operation["startSeq"])
                last = seqs.index(operation["endSeq"])
            except (KeyError, ValueError) as exc:
                raise SessionMigrateError("invalid DSH surface replacement range") from exc
            if last < first or not set(seqs[first : last + 1]).issubset(
                _source_seqs(row.get("sourceEventSeqs"), index)
            ):
                raise SessionMigrateError("invalid DSH replacement provenance")
            surface[first : last + 1] = [row]
        else:
            raise SessionMigrateError("DSH surface event needs append or replace")
    events: list[Event] = []
    for row in surface:
        kind, data = row["type"], row["data"]
        message = data if kind == "user/message" else _object(data.get("message"), "message")
        role = {
            "user/message": Role.USER,
            "assistant/message": Role.ASSISTANT,
            "tool/result": Role.TOOL,
            "system/message": Role.SYSTEM,
        }.get(kind)
        timestamp = _ms(row["time"])
        source = _object(message.get("source"), "message source")
        if _text(source.get("kind"), "source kind") == "plugin":
            raise SessionMigrateError("DSH v4 does not accept the retired plugin source kind")
        if role is not None and message.get("role") != role.value:
            raise SessionMigrateError("DSH message role does not match its event type")
        if kind == "assistant/message":
            model = source.get("model") or model
            provider = source.get("provider") or provider
        content = message.get("content")
        if not isinstance(content, list):
            raise SessionMigrateError("DSH message content must be a block array")
        provenance = Provenance(row["seq"] + 1, kind)
        if role == Role.TOOL:
            if (
                source.get("kind") != "tool"
                or source.get("callId") != message.get("toolCallId")
                or ("isError" in message and type(message["isError"]) is not bool)
            ):
                raise SessionMigrateError("DSH tool result identity or error flag is invalid")
            blocks = [_object(block, "tool result block") for block in content]
            events.append(
                Event(
                    EventKind.TOOL_RESULT,
                    provenance,
                    role,
                    timestamp,
                    text="\n".join(
                        _text(b.get("text"), "result text")
                        for b in blocks
                        if b.get("type") == "text" and b.get("text")
                    ),
                    tool_call_id=_text(message.get("toolCallId"), "result call id"),
                    payload={"content_blocks": blocks, "is_error": message.get("isError") is True},
                )
            )
            continue
        for block_index, block in enumerate(content):
            block = _object(block, "content block")
            block_type = _text(block.get("type"), "block type")
            provenance = Provenance(row["seq"] + 1, kind, block_index=block_index)
            if block_type == "text" and isinstance(block.get("text"), str):
                events.append(
                    Event(EventKind.MESSAGE, provenance, role, timestamp, text=block["text"])
                )
            elif block_type == "tool-call" and role == Role.ASSISTANT:
                arguments = _text(block.get("arguments"), "tool arguments")
                try:
                    arguments = json.loads(arguments, parse_constant=_reject_constant)
                except json.JSONDecodeError as exc:
                    raise SessionMigrateError("DSH tool arguments are not valid JSON") from exc
                events.append(
                    Event(
                        EventKind.TOOL_CALL,
                        provenance,
                        Role.ASSISTANT,
                        timestamp,
                        tool_name=_text(block.get("name"), "tool name"),
                        tool_call_id=_text(block.get("id"), "tool call id"),
                        payload={"input": arguments},
                    )
                )
            else:
                events.append(
                    Event(
                        EventKind.OPAQUE,
                        provenance,
                        role,
                        timestamp,
                        payload={"reason": f"dsh:block:{block_type}"},
                    )
                )
    return Session(
        source_format=DshFormat.DSH,  # type: ignore[arg-type] -- upstream uses the .value protocol.
        source_path=path.resolve(),
        source_sha256=digest,
        session_id=session_id,
        cwd=Path(cwd) if cwd else None,
        started_at=started_at,
        cli_version=None,
        model=model,
        model_provider=provider,
        title=title,
        events=tuple(events + opaque),
        raw_record_count=len(records),
    )


def serialize(
    session: Session, *, session_id: str, cwd: Path, provider: str, model: str | None
) -> tuple[bytes, dict[str, int]]:
    portable, losses = pi.serialize(
        session, session_id=session_id, cwd=cwd, provider=provider, model=model
    )
    rows = [json.loads(line) for line in portable.splitlines()]
    timestamp = rows[0]["timestamp"]
    created = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
    header = {
        "type": "session",
        "version": VERSION,
        "id": session_id,
        "createdAt": created,
        "cwd": str(cwd),
        "isSeeded": False,
        "delegationDepth": 0,
    }
    events: list[dict[str, Any]] = []
    active_surface: list[int] = []
    dropped = Counter(losses)
    turn = step = 0
    in_turn = in_step = False
    pending: dict[str, str] = {}

    def push(kind: str, time: int, data: dict, *, surface: bool = False) -> None:
        if surface:
            active_surface.append(len(events))
        events.append(
            {
                "type": kind,
                "seq": len(events),
                "time": time,
                "data": data,
                **({"surfaceOp": "append"} if surface else {}),
            }
        )

    def close_step(time: int) -> None:
        nonlocal in_step
        if pending:
            raise SessionMigrateError("unfinished tool calls cannot form a resumable DSH step")
        if in_step:
            push("step/end", time, {"turn": turn, "step": step})
            in_step = False

    def close_turn(time: int) -> None:
        nonlocal in_turn
        close_step(time)
        if in_turn:
            push("turn/end", time, {"turn": turn, "reason": {"kind": "completed"}})
            in_turn = False

    last_time = created
    for row in rows[1:]:
        kind = row["type"]
        if kind == "session_info":
            continue
        if kind == "compaction":
            # Preserve the summary as context; surface replacement keeps old logs off the model.
            message = {
                "role": "user",
                "content": "[Migrated compaction summary]\n" + row["summary"],
                "timestamp": last_time,
            }
            dropped["compaction:converted_to_user_context"] += 1
        elif kind == "message":
            message = row["message"]
        else:
            dropped[f"pi_intermediate:{kind}"] += 1
            continue
        time = max(last_time, message["timestamp"])
        last_time = time
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        blocks = []
        for block in content:
            if block["type"] == "text":
                blocks.append(block)
            elif block["type"] == "toolCall" and role == "assistant":
                if block["id"] in pending:
                    raise SessionMigrateError("duplicate tool call id in DSH output")
                arguments = json.dumps(
                    block["arguments"], ensure_ascii=False, separators=(",", ":")
                )
                blocks.append(
                    {
                        "type": "tool-call",
                        "id": block["id"],
                        "name": block["name"],
                        "arguments": arguments,
                    }
                )
            else:
                dropped[f"dsh:unsupported_block:{block['type']}"] += 1
        if role == "user":
            close_turn(time)
            turn, step = turn + 1, 0
            push("turn/start", time, {"turn": turn})
            in_turn = True
            replaced = active_surface.copy() if kind == "compaction" else []
            push(
                "user/message",
                time,
                {
                    "id": str(uuid.uuid4()),
                    "role": "user",
                    "source": {"kind": "user"},
                    "content": blocks,
                },
                surface=True,
            )
            if replaced:
                events[-1]["surfaceOp"] = {
                    "op": "replace",
                    "startSeq": replaced[0],
                    "endSeq": replaced[-1],
                }
                events[-1]["sourceEventSeqs"] = replaced
                active_surface[:] = [events[-1]["seq"]]
        elif role == "assistant":
            close_step(time)
            if not in_turn:
                turn += 1
                push("turn/start", time, {"turn": turn})
                in_turn = True
            step += 1
            push("step/start", time, {"turn": turn, "step": step})
            in_step = True
            push(
                "assistant/message",
                time,
                {
                    "turn": turn,
                    "step": step,
                    "stream": [],
                    "message": {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": blocks,
                        "source": {
                            "kind": "model",
                            "provider": provider,
                            "model": model or "imported",
                        },
                    },
                },
                surface=True,
            )
            for block in blocks:
                if block["type"] == "tool-call":
                    if block["id"] in pending:
                        raise SessionMigrateError("duplicate tool call id in DSH output")
                    pending[block["id"]] = block["name"]
                    push(
                        "tool/call",
                        time,
                        {
                            "turn": turn,
                            "step": step,
                            "callId": block["id"],
                            "name": block["name"],
                            "arguments": block["arguments"],
                        },
                    )
        elif role == "toolResult":
            call_id = message["toolCallId"]
            if call_id not in pending:
                raise SessionMigrateError("orphan tool result cannot form a resumable DSH session")
            pending.pop(call_id)
            push(
                "tool/result",
                time,
                {
                    "turn": turn,
                    "step": step,
                    "message": {
                        "id": str(uuid.uuid4()),
                        "role": "tool",
                        "source": {"kind": "tool", "callId": call_id},
                        "toolCallId": call_id,
                        "isError": message["isError"],
                        "content": blocks,
                    },
                },
                surface=True,
            )
    close_turn(last_time)
    if session.title:
        push(
            "session/title",
            last_time,
            {"title": session.title, "messageSeqs": [], "source": {"kind": "user"}},
        )

    def encode(records: list[dict]) -> bytes:
        return (
            "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in records)
            + "\n"
        ).encode("utf-8")

    options = {zstd.CompressionParameter.checksum_flag: 1}
    return (
        zstd.compress(encode([header]), options=options)
        + zstd.compress(encode(events), options=options)
    ), dict(sorted(dropped.items()))


def project_key(cwd: Path) -> str:
    # DSH escapes JS UTF-16 units, including supplementary characters.
    units = str(cwd).encode("utf-16-le", errors="surrogatepass")
    text = "".join(chr(int.from_bytes(units[i : i + 2], "little")) for i in range(0, len(units), 2))
    text = re.sub(r"[/\\:]+", "-", text)
    slug = (
        "".join(
            ch if re.fullmatch(r"[A-Za-z0-9._-]", ch) else f"~{ord(ch):04X}" for ch in text
        ).lstrip("-")
        or "root"
    )
    return f"--{slug[:251]}--"
