from __future__ import annotations

import json
from pathlib import Path

import pytest
from session_migrate.errors import SessionMigrateError
from session_migrate.model import EventKind

from agent_session_bridge import core
from agent_session_bridge.cli import main

IDENTIFIER = "8dbe1cda-7a47-4ebd-b211-ff785124165e"


def transcript(tmp_path: Path, extra: dict | None = None) -> Path:
    rows = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "<runtime>PRIVATE</runtime><user_query>检查 café.py</user_query>",
                }
            ],
        },
        {"type": "function_call", "name": "bash", "arguments": "PRIVATE_COMMAND"},
        {"type": "function_call_result", "output": "PRIVATE_OUTPUT"},
        {"type": "reasoning", "content": "PRIVATE_THINKING"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "修好了 ✓"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "<cb_summary>保留上下文</cb_summary>"}],
        },
        {"type": "ai-title", "aiTitle": "Synthetic WorkBuddy"},
    ]
    if extra is not None:
        rows.append(extra)
    for row in rows:
        row.setdefault("sessionId", IDENTIFIER)
        row.setdefault("cwd", str(tmp_path))
        row.setdefault("timestamp", 1792022400000)
    path = tmp_path / "workbuddy.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


def test_workbuddy_source_to_pi_filters_runtime_and_tools(tmp_path):
    path = transcript(tmp_path)
    source = core.load(path)
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    core.apply(source, payload, output, manifest)
    assert source.source_format.value == "workbuddy"
    assert b"PRIVATE" not in payload
    assert manifest["dropped_events"]["filtered:tool_call"] == 1
    assert manifest["dropped_events"]["filtered:tool_result"] == 1
    assert manifest["dropped_events"]["filtered:thinking"] == 1
    events = core.load(output).events
    assert [e.text for e in events if e.kind == EventKind.COMPACTION] == ["保留上下文"]
    assert path.exists()


def test_workbuddy_portable_export_and_uninitialized_native_home(tmp_path):
    source = core.load(transcript(tmp_path))
    with pytest.raises(SessionMigrateError, match="DB missing"):
        core.prepare(source, target="workbuddy", target_home=tmp_path / "empty-home")
    payload, output, manifest = core.prepare(
        source, target="workbuddy", output_dir=tmp_path / "out"
    )
    result = core.apply(source, payload, output, manifest)
    assert result["resume"] == []
    assert manifest["target"]["store"] is None
    assert b"PRIVATE" not in payload
    assert core.load(output).session_id == result["session_id"]
    result = core.apply(source, payload, tmp_path / "move.jsonl", manifest, move=True)
    assert result["status"] == "moved"
    assert not source.source_path.exists()


@pytest.mark.parametrize(
    "extra",
    [
        {"type": "message", "role": "unknown", "content": "bad"},
        {"type": "message", "role": "user", "content": [{"type": "text", "text": 1}]},
        {"type": "message", "role": "user", "content": {"text": "bad"}},
        {"type": "message", "role": "user", "content": "x", "sessionId": "other"},
        {"type": "message", "role": "user", "content": "x", "timestamp": -1},
        {"type": [], "content": []},
    ],
)
def test_workbuddy_malformed_input_is_rejected(tmp_path, extra):
    with pytest.raises(SessionMigrateError):
        core.load(transcript(tmp_path, extra), "workbuddy")


def test_workbuddy_project_discovery_uses_config_override(tmp_path, monkeypatch, capsys):
    home = tmp_path / "wb-home"
    bucket = home / "projects" / "example"
    bucket.mkdir(parents=True)
    transcript(bucket)
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(home))
    assert main(["list", "--from", "workbuddy", "--project", str(bucket)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["count"] == 1 and result["results"][0]["session_id"] == IDENTIFIER
    assert "PRIVATE" not in json.dumps(result)
