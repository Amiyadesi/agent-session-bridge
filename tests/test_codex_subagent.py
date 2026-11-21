"""Codex paginated subagent rollouts: inherited parent context stays out of the migration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from session_migrate.errors import SessionMigrateError
from session_migrate.model import EventKind, Role

from agent_session_bridge import core
from agent_session_bridge.cli import main

STAMP = "2026-11-05T03:59:45Z"
CHILD_ID = "01a1a9dc-dafa-7e61-9da4-5f0c2d9d1e77"
PARENT_ID = "01a1a9d0-11aa-7c22-8b33-4d5e6f708192"


def item(kind: str, text: str) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {"type": kind, "content": [{"type": "text", "text": text}]},
        },
    }


def rollout(directory: Path, *, boundary: object = 4, mode: str = "paginated", keep: int = 7):
    directory.mkdir(parents=True, exist_ok=True)
    meta = {"id": CHILD_ID, "cwd": str(directory), "timestamp": STAMP, "history_mode": mode}
    if boundary is not None:
        meta["subagent_history_start_ordinal"] = boundary
    records = [
        {"type": "session_meta", "payload": meta},
        # Inherited parent context, copied verbatim ahead of the child's own history.
        {
            "type": "session_meta",
            "payload": {
                "id": PARENT_ID,
                "cwd": str(directory / "parent"),
                "history_mode": "paginated",
                "subagent_history_start_ordinal": 2,
                "history_base": {"thread_id": PARENT_ID, "ordinal": 1},
            },
        },
        item("UserMessage", "parent-only request"),
        item("AgentMessage", "parent-only answer"),
        item("UserMessage", "child task"),
        item("AgentMessage", "child answer"),
        {"type": "event_msg", "payload": {"type": "thread_name_updated", "thread_name": "Child"}},
    ][:keep]
    path = directory / f"rollout-2026-11-05T11-59-45-{CHILD_ID}.jsonl"
    path.write_text(
        "".join(
            json.dumps(record | {"timestamp": STAMP, "ordinal": ordinal}) + "\n"
            for ordinal, record in enumerate(records)
        ),
        encoding="utf-8",
    )
    return path


def messages(session) -> list[tuple[Role, str]]:
    return [(e.role, e.text) for e in session.events if e.kind == EventKind.MESSAGE]


@pytest.mark.parametrize("source_format", ["codex", "auto"])
def test_subagent_rollout_projects_only_its_own_history(tmp_path, source_format):
    path = rollout(tmp_path / "codex")
    original = path.read_bytes()
    session = core.load(path, source_format)
    assert messages(session) == [(Role.USER, "child task"), (Role.ASSISTANT, "child answer")]
    assert (session.session_id, session.title, session.cwd) == (CHILD_ID, "Child", path.parent)
    assert session.source_path == path.resolve() and session.raw_record_count == 7
    assert session.source_sha256 == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original
    payload, _, manifest = core.prepare(session, target="opencode", output_dir=tmp_path / "out")
    assert manifest["dropped_events"]["filtered:inherited_subagent_context"] == 3
    assert b"child task" in payload and b"parent-only" not in payload


def test_subagent_rollout_converts_from_cli(tmp_path, capsys):
    root = tmp_path / "codex"
    rollout(root)
    argv = ["convert", "--from", "codex", "--to", "opencode", "--source-root", str(root)]
    argv += ["--project", str(root), "--output-dir", str(tmp_path / "out")]
    assert main(argv) == 0
    preview = capsys.readouterr().out
    assert json.loads(preview)["count"] == 1 and not (tmp_path / "out").exists()
    assert "child task" not in preview and "parent-only" not in preview
    assert main([*argv, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["count"] == 1 and not applied["errors"]
    written = b"".join(p.read_bytes() for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert b"child answer" in written and b"parent-only" not in written


def test_subagent_without_own_history_and_root_rollout_still_load(tmp_path):
    assert messages(core.load(rollout(tmp_path / "empty", keep=4), "codex")) == []
    root = core.load(rollout(tmp_path / "root", boundary=None, keep=1), "codex")
    assert root.session_id == CHILD_ID


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"keep": 3}, "incomplete"),
        ({"boundary": 0}, "positive ordinal"),
        ({"boundary": "4"}, "positive ordinal"),
        ({"boundary": True}, "positive ordinal"),
        ({"mode": "legacy"}, "requires paginated history"),
    ],
)
def test_invalid_subagent_boundary_fails_closed(tmp_path, options, error):
    path = rollout(tmp_path / "codex", **options)
    with pytest.raises(SessionMigrateError, match=error):
        core.load(path, "codex")
