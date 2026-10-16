"""Antigravity CLI metadata and Windows SQLite lifecycle fixes."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity as native
from session_migrate.model import EventKind, Role


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _connect(*args, **kwargs):
    return sqlite3.connect(*args, **kwargs, factory=_ClosingConnection)


# shortcut: 0.11.0 leaks SQLite handles on Windows; remove when upstream closes them.
# Only this codec's SQLite binding changes; its schema and executable gates stay intact.
native.sqlite3 = SimpleNamespace(**(vars(sqlite3) | {"connect": _connect}))


def validate_summary(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)")}
    if not set(native._SUMMARY_COLUMNS) <= columns:
        raise SessionMigrateError("unsupported Antigravity conversation_summaries schema")
    if conn.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise SessionMigrateError("Antigravity summary database failed integrity check")


def parse(path: Path):
    if path.suffix == ".pb":
        raise SessionMigrateError(
            "Antigravity desktop .pb is unsupported; use agy /resume's Antigravity tab "
            "to clone it into a CLI conversation first"
        )
    session = native.parse_session(path)
    summary = path.parent.parent / "conversation_summaries.db"
    if path.parent.name == "conversations" and summary.is_file():
        with closing(sqlite3.connect(summary.resolve().as_uri() + "?mode=ro", uri=True)) as c:
            validate_summary(c)
            row = c.execute(
                "SELECT title,preview,workspace_uris FROM conversation_summaries "
                "WHERE conversation_id=?",
                (session.session_id,),
            ).fetchone()
        if row is not None:
            title, preview, raw_uris = row
            if not all(isinstance(value, str) for value in row):
                raise SessionMigrateError("invalid Antigravity title/workspace metadata")
            uris = json.loads(raw_uris) if raw_uris else []
            if not isinstance(uris, list) or not all(isinstance(uri, str) for uri in uris):
                raise SessionMigrateError("invalid Antigravity title/workspace metadata")
            cwd = Path.from_uri(uris[0]) if uris else None
            return replace(session, title=title or preview or None, cwd=cwd)
    manifest = path.with_name(path.name + ".manifest.json")
    if manifest.is_file():
        value = json.loads(manifest.read_bytes())
        target = value.get("target") if isinstance(value, dict) else None
        if (
            not isinstance(target, dict) or target.get("format") != "antigravity"
            or target.get("session_id") != session.session_id
        ):
            raise SessionMigrateError("Antigravity manifest identity does not match conversation")
        title, cwd = target.get("title"), target.get("cwd")
        if title is not None and not isinstance(title, str) or not isinstance(cwd, str):
            raise SessionMigrateError("invalid Antigravity manifest title/cwd")
        session = replace(session, title=title, cwd=Path(cwd))
    return session


def conversation(session):
    events, dropped = [], Counter()
    for event in session.events:
        if event.kind == EventKind.COMPACTION:
            event = replace(
                event, kind=EventKind.MESSAGE, role=Role.USER,
                text="[Imported conversation summary]\n" + (event.text or ""), payload={},
            )
            dropped["compaction:converted_to_user_context"] += 1
        elif event.kind == EventKind.MESSAGE and not event.text:
            dropped["message:empty"] += 1
        events.append(event)
    return replace(session, events=tuple(events)), dropped


def with_workspace(data: bytes, cwd: Path) -> bytes:
    # The runtime rebuilds SQL workspace_uris from trajectory metadata on resume.
    with closing(sqlite3.connect(":memory:")) as c, c:
        c.deserialize(data)
        # Version 0 triggers the client's legacy migration, which clears imported metadata.
        c.execute("PRAGMA user_version=1")
        metadata = c.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE id='main'"
        ).fetchone()[0]
        c.execute(
            "UPDATE trajectory_metadata_blob SET data=? WHERE id='main'",
            (metadata + native._field_bytes(1, native._field_text(1, cwd.as_uri()))
             + native._field_text(7, cwd.as_uri()),),
        )
        c.commit()
        return c.serialize()


def raw_summary(data: bytes, values: dict) -> bytes:
    # Keep the 1.4.0 list cache consistent with the separate annotations file.
    with native._database_from_bytes(data) as c:
        metadata = c.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE id='main'"
        ).fetchone()[0]
        trajectory_id = c.execute(
            "SELECT trajectory_id FROM trajectory_meta WHERE cascade_id=?",
            (values["conversation_id"],),
        ).fetchone()[0]
    timestamp = native._field_varint(
        1, int(native._parse_timestamp(values["last_modified_time"]).timestamp())
    )
    return (
        native._field_text(1, values["title"])
        + native._field_varint(2, values["step_count"])
        + native._field_bytes(3, timestamp)
        + native._field_text(4, trajectory_id)
        + native._field_varint(5, 1)
        + native._field_bytes(10, timestamp)
        + native._field_bytes(15, native._field_text(1, values["title"]))
        + native._field_varint(16, values["last_user_input_step_index"])
        + native._field_bytes(17, metadata)
        + native._field_varint(20, 17)
        + native._field_varint(22, 4)
    )


def annotation_path(home: Path, session_id: str) -> Path:
    return home / "annotations" / f"{session_id}.pbtxt"


def annotation_bytes(title: str | None) -> bytes:
    value = "title: " + json.dumps(title or "Imported conversation", ensure_ascii=False) + "\n"
    return value.encode("utf-8")


def summary_cache(data: bytes, session_id: str, summary: bytes | None = None) -> bytes:
    entries, seen = [], set()
    for field in native._decode_message(data):
        if field.number != 1 or field.wire_type != 2:
            raise SessionMigrateError("unsupported Antigravity summary cache schema")
        entry = native._decode_message(field.value)
        key = native._required_text(entry, 1, "summary cache conversation ID")
        native._required_bytes(entry, 2, "summary cache value")
        if key in seen or any(f.number not in {1, 2} for f in entry):
            raise SessionMigrateError("ambiguous Antigravity summary cache entry")
        seen.add(key)
        if key != session_id:
            entries.append(native._field_bytes(1, field.value))
        elif summary is not None:
            raise SessionMigrateError("Antigravity target already exists in summary cache")
    if summary is not None:
        entries.append(native._field_bytes(
            1, native._field_text(1, session_id) + native._field_bytes(2, summary)
        ))
    return b"".join(entries)


def ensure_closed(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise SessionMigrateError("close the source Antigravity client before cutting its database")


def ensure_store_closed(home: Path) -> None:
    for path in (home / "presence").glob("*.lock"):
        try:
            with path.open("r+b") as stream:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise SessionMigrateError(
                "close the Antigravity client before modifying its store"
            ) from exc


def move_bytes(path: Path) -> bytes:
    ensure_closed(path)
    if path.parent.name == "conversations":
        ensure_store_closed(path.parent.parent)
    data = native.snapshot_database_bytes(path)
    ensure_closed(path)
    return data
