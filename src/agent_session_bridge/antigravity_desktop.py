"""Antigravity IDE 2.5.5 SQLite conversations and desktop history indexes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.model import Session

from agent_session_bridge import antigravity

PB_KEY = "antigravityUnifiedStateSync.trajectorySummaries"
JSON_KEY = "chat.ChatSessionStore.index"
CACHES = ("jetbox_summaries_proto.pb", "agyhub_summaries_proto.pb")


class DesktopFormat(StrEnum):
    DESKTOP = "antigravity-desktop"


@dataclass(frozen=True, slots=True)
class DesktopSession(Session):
    state_database: Path | None = None


def home() -> Path:
    current = Path.home() / ".gemini" / "antigravity-ide"
    legacy = Path.home() / ".gemini" / "antigravity"
    if not current.exists() and any((legacy / "conversations").glob("*.db")):
        return legacy
    return current


def state_path(data_home: Path, explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser().resolve()
    data_home = data_home.expanduser().resolve()
    if data_home.parent.name != ".gemini":
        return None
    process_home = data_home.parent.parent
    app_name = "Antigravity IDE" if data_home.name == "antigravity-ide" else "Antigravity"
    if sys.platform == "win32":
        base = process_home / "AppData" / "Roaming"
        if process_home == Path.home().resolve() and os.environ.get("APPDATA"):
            base = Path(os.environ["APPDATA"])
    elif sys.platform == "darwin":
        base = process_home / "Library" / "Application Support"
    else:
        base = process_home / ".config"
        if process_home == Path.home().resolve() and os.environ.get("XDG_CONFIG_HOME"):
            base = Path(os.environ["XDG_CONFIG_HOME"])
    return base / app_name / "User" / "globalStorage" / "state.vscdb"


def detect(path: Path) -> bool:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("PRAGMA trusted_schema=OFF")
        return bool(c.execute(
            "SELECT 1 FROM trajectory_meta WHERE trajectory_type=4 AND source=1"
        ).fetchone())


def _wire_bytes(fields) -> bytes:
    native = antigravity.native
    return b"".join(
        native._field_varint(f.number, f.value) if f.wire_type == 0
        else native._field_bytes(f.number, f.value) if f.wire_type == 2
        else native._varint((f.number << 3) | f.wire_type) + f.value
        for f in fields
    )


def codec_bytes(data: bytes) -> bytes:
    # Normalize desktop enums and text shapes in a private snapshot for the shared projector.
    native = antigravity.native
    with closing(sqlite3.connect(":memory:")) as c:
        c.deserialize(data)
        c.execute("PRAGMA trusted_schema=OFF")
        antigravity.native._validate_conversation_schema(c)
        rows = c.execute(
            "SELECT trajectory_id FROM trajectory_meta WHERE trajectory_type=4 AND source=1"
        ).fetchall()
        if len(rows) != 1:
            raise SessionMigrateError("expected one Antigravity desktop cascade trajectory")
        c.execute("UPDATE trajectory_meta SET source=17 WHERE trajectory_id=?", rows[0])
        for index, kind, payload in c.execute(
            "SELECT idx,step_type,step_payload FROM steps WHERE step_type IN (14,15)"
        ).fetchall():
            field = native._STEP_PAYLOAD_FIELDS[kind]
            outer = native._decode_message(payload)
            nested = native._decode_message(native._required_bytes(outer, field, "step payload"))
            text = None
            if kind == native.STEP_TYPE_USER_INPUT:
                if not (native._optional_text(nested, 2) or native._optional_text(nested, 1)):
                    text = "".join(
                        native._optional_text(native._decode_message(item), 1) or ""
                        for item in native._bytes_values(nested, 3)
                    ) or None
                text_field = 2
            else:
                text = native._optional_text(nested, 8)
                text_field = 1
            if text is not None:
                normalized = _wire_bytes(f for f in nested if f.number != text_field)
                normalized += native._field_text(text_field, text)
                payload = _wire_bytes(
                    replace(f, value=normalized) if f.number == field else f for f in outer
                )
                c.execute("UPDATE steps SET step_payload=? WHERE idx=?", (payload, index))
        c.commit()
        return c.serialize()


def as_desktop(data: bytes) -> bytes:
    with closing(sqlite3.connect(":memory:")) as c:
        c.deserialize(data)
        c.execute("UPDATE trajectory_meta SET source=1 WHERE trajectory_type=4 AND source=17")
        metadata = c.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE id='main'"
        ).fetchone()[0]
        # CLI project IDs have no corresponding project in the desktop application.
        metadata = _wire_bytes(
            f for f in antigravity.native._decode_message(metadata) if f.number != 18
        )
        c.execute("UPDATE trajectory_metadata_blob SET data=? WHERE id='main'", (metadata,))
        c.commit()
        return c.serialize()


def validate_state(c) -> None:
    if not {"key", "value"} <= {r[1] for r in c.execute("PRAGMA table_info(ItemTable)")}:
        raise SessionMigrateError("unsupported Antigravity desktop state.vscdb schema")
    if c.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise SessionMigrateError("Antigravity desktop state database failed integrity check")


def _value(c, key: str) -> str | None:
    rows = c.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchall()
    if len(rows) > 1:
        raise SessionMigrateError("duplicate Antigravity desktop history index")
    if not rows:
        return None
    value = rows[0][0]
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise SessionMigrateError("invalid Antigravity desktop history index")
    return value


def indexes(c) -> tuple[bytes, dict]:
    validate_state(c)
    encoded = _value(c, PB_KEY)
    raw = base64.b64decode(encoded or "", validate=True)
    antigravity.summary_cache(raw, "")
    value = _value(c, JSON_KEY)
    index = json.loads(value) if value else {"version": 1, "entries": {}}
    if (
        not isinstance(index, dict) or index.get("version") != 1
        or not isinstance(index.get("entries"), dict)
        or not all(isinstance(v, dict) for v in index["entries"].values())
    ):
        raise SessionMigrateError("unsupported Antigravity desktop chat index")
    return raw, index


def parse(path: Path, *, state_database: Path | None = None) -> DesktopSession:
    if path.suffix != ".db":
        raise SessionMigrateError(
            "legacy Antigravity desktop .pb is unsupported; upgrade/import it in the "
            "official desktop client to SQLite .db first"
        )
    native = antigravity.native
    data = native.snapshot_database_bytes(path)
    parsed = native._parse_database_bytes(codec_bytes(data), expected_session_id=None)
    with native._database_from_bytes(data) as c:
        metadata = c.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE id='main'"
        ).fetchone()[0]
    uris = native._bytes_values(native._decode_message(metadata), 7)
    cwd = Path.from_uri(uris[0].decode("utf-8")) if uris else None
    data_home = path.parent.parent if path.parent.name == "conversations" else path.parent
    state_database = state_path(data_home, state_database)
    title = None
    if state_database is not None and state_database.is_file():
        with closing(sqlite3.connect(state_database.as_uri() + "?mode=ro", uri=True)) as c:
            raw, index = indexes(c)
        title = index["entries"].get(parsed.session_id, {}).get("title")
        for field in native._decode_message(raw):
            entry = native._decode_message(field.value)
            if native._required_text(entry, 1, "conversation ID") == parsed.session_id:
                summary = native._decode_message(native._required_bytes(entry, 2, "summary"))
                title = native._optional_text(summary, 1) or title
    annotation = antigravity.annotation_path(data_home, parsed.session_id)
    if annotation.is_file():
        text = annotation.read_text(encoding="utf-8")
        if match := re.search(r"(?m)^title\s*:\s*", text):
            title, _ = json.JSONDecoder().raw_decode(text[match.end():])
    manifest = path.with_name(path.name + ".manifest.json")
    if manifest.is_file():
        value = json.loads(manifest.read_bytes())
        target = value.get("target") if isinstance(value, dict) else None
        if not isinstance(target, dict):
            raise SessionMigrateError("invalid Antigravity desktop manifest")
        if (
            target.get("format") != "antigravity-desktop"
            or target.get("session_id") != parsed.session_id
        ):
            raise SessionMigrateError("Antigravity desktop manifest identity mismatch")
        title = title or target.get("title")
    if title is not None and not isinstance(title, str):
        raise SessionMigrateError("invalid Antigravity desktop title")
    return DesktopSession(
        source_format=DesktopFormat.DESKTOP, source_path=path.resolve(),
        source_sha256=hashlib.sha256(data).hexdigest(), session_id=parsed.session_id,
        cwd=cwd, started_at=parsed.started_at, cli_version="IDE 2.5.5",
        model=None, title=title, events=parsed.events, raw_record_count=parsed.raw_record_count,
        state_database=state_database,
    )


def descriptor(data_home: Path, state_database: Path | None = None) -> dict:
    from agent_session_bridge import stores

    data_home = data_home.expanduser().resolve()
    if not (data_home / "installation_id").is_file() or not (data_home / "conversations").is_dir():
        raise SessionMigrateError("initialize the Antigravity desktop client in the target home")
    state_database = state_path(data_home, state_database)
    if state_database is None or not state_database.is_file():
        raise SessionMigrateError(
            "Antigravity desktop state.vscdb missing; pass --source-state-db / --target-state-db"
        )
    with stores.connection(state_database) as c:
        indexes(c)
    return {"kind": "antigravity-desktop", "home": str(data_home),
            "database": str(state_database)}


def update_index(
    c, sid: str, summary: bytes | None = None, *, title: str = "", now: int = 0
) -> None:
    from agent_session_bridge import stores

    raw, index = indexes(c)
    updated = antigravity.summary_cache(raw, sid, summary)
    entries = index["entries"]
    if summary is None:
        if sid not in entries and updated == raw:
            raise SessionMigrateError("source desktop index row missing; source retained")
        entries.pop(sid, None)
    else:
        if sid in entries:
            raise SessionMigrateError("Antigravity desktop target already exists in history")
        entries[sid] = {"sessionId": sid, "title": title, "lastModified": now, "isStale": False}
    for key, value in (
        (PB_KEY, base64.b64encode(updated).decode("ascii")),
        (JSON_KEY, json.dumps(index, ensure_ascii=False)),
    ):
        if _value(c, key) is None:
            stores.insert(c, "ItemTable", {"key": key, "value": value})
        else:
            c.execute("UPDATE ItemTable SET value=? WHERE key=?", (value, key))


def cache_changes(data_home: Path, sid: str, summary: bytes | None = None) -> list:
    changes = []
    for name in CACHES:
        path = data_home / name
        if path.exists():
            original = path.read_bytes()
            updated = antigravity.summary_cache(original, sid, summary)
            if updated != original:
                changes.append((path, original, updated))
    return changes


def register(store, target, payload, title, now):
    from agent_session_bridge import stores

    data_home = Path(store["home"])
    ensure_closed(data_home, Path(store["database"]))
    native = antigravity.native
    canonical = codec_bytes(payload)
    with native._database_from_bytes(canonical) as c:
        count = c.execute("SELECT count(*) FROM steps").fetchone()[0]
    values = {"conversation_id": target["session_id"], "title": title, "step_count": count,
              "last_modified_time": datetime.fromtimestamp(now / 1000, UTC).isoformat(),
              "last_user_input_step_index": native._last_user_input_index(canonical)}
    raw = antigravity.raw_summary(payload, values)
    changes = cache_changes(data_home, target["session_id"], raw)
    written = []
    try:
        with stores.connection(Path(store["database"]), write=True) as c:
            update_index(c, target["session_id"], raw, title=title, now=now)
            for path, original, updated in changes:
                if path.read_bytes() != original:
                    raise SessionMigrateError("desktop cache changed; close the client and retry")
                stores._replace(path, updated)
                written.append((path, original, updated))
    except BaseException:
        for path, original, updated in reversed(written):
            if path.read_bytes() == updated:
                stores._replace(path, original)
        raise


def ensure_closed(data_home: Path, state_database: Path | None) -> None:
    antigravity.ensure_store_closed(data_home)
    if state_database is not None:
        antigravity.ensure_closed(state_database)
    for path in (data_home / "daemon").glob("*.json"):
        pid = json.loads(path.read_bytes()).get("pid")
        if type(pid) is not int or pid <= 0:
            raise SessionMigrateError("invalid Antigravity desktop daemon discovery file")
        if sys.platform == "win32":
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.CloseHandle(handle)
                raise SessionMigrateError(
                    "close the Antigravity desktop client before modifying its store"
                )
            if ctypes.get_last_error() != 87:
                raise SessionMigrateError(
                    "cannot check Antigravity desktop process; source retained"
                )
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise SessionMigrateError("cannot check Antigravity desktop process") from exc
            raise SessionMigrateError(
                "close the Antigravity desktop client before modifying its store"
            )
