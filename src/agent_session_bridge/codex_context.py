"""Codex subagent rollouts store inherited parent context before their own history."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from session_migrate.conversion import load_session
from session_migrate.errors import SessionMigrateError
from session_migrate.jsonl import (
    JsonlError,
    encode_jsonl,
    ensure_file_unchanged,
    file_sha256,
    file_snapshot,
    iter_jsonl,
)
from session_migrate.model import AgentFormat, Session

BOUNDARY = "subagent_history_start_ordinal"
INHERITED = "inherited_subagent_context"


def _head(record: dict) -> dict | None:
    payload = record.get("payload")
    if record.get("type") == "session_meta" and isinstance(payload, dict):
        return payload
    return None


def _is_subagent(path: Path) -> bool:
    # Codex reads the boundary from the rollout head only.
    try:
        first = next(iter_jsonl(path), None)
    except JsonlError:
        return False
    head = _head(first.value) if first else None
    return bool(head) and head.get(BOUNDARY) is not None


def parse(path: Path, source_format: AgentFormat | None) -> Session:
    """Project a paginated subagent rollout the way Codex does: its own records only."""
    if not _is_subagent(path):
        return load_session(path, source_format)
    before = file_snapshot(path)
    records = [record.value for record in iter_jsonl(path)]
    head = _head(records[0])
    start = head.pop(BOUNDARY)
    if isinstance(start, bool) or not isinstance(start, int) or start < 1:
        raise SessionMigrateError("Codex subagent history boundary must be a positive ordinal")
    if head.get("history_mode") != "paginated":
        raise SessionMigrateError("Codex subagent history boundary requires paginated history")
    if len(records) < start:
        # Codex refuses to resume a child whose inherited prefix was not fully written.
        raise SessionMigrateError(
            "Codex subagent rollout is incomplete: inherited context ends before "
            f"ordinal {start}"
        )
    # The pinned parser has no subagent projection, so it gets the child's records plus one
    # content-free placeholder per inherited record. Ordinals stay for its continuity check.
    for index in range(1, start):
        records[index] = {
            key: value
            for key, value in records[index].items()
            if key in {"timestamp", "ordinal"}
        } | {"type": INHERITED}
    with tempfile.TemporaryDirectory(prefix="asb-codex-") as root:
        projected = Path(root) / path.name
        projected.write_bytes(encode_jsonl(records))
        session = load_session(projected, AgentFormat.CODEX)
    digest = file_sha256(path)
    ensure_file_unchanged(path, before)
    return replace(session, source_path=path.resolve(), source_sha256=digest)
