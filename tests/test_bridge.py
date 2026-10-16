from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import pi
from session_migrate.model import (
    AgentFormat,
    Event,
    EventKind,
    Provenance,
    Role,
    Session,
    TargetFormat,
)

from agent_session_bridge import antigravity, antigravity_desktop, core, dsh, workbuddy
from agent_session_bridge.cli import main

STAMP = "2026-10-15T00:00:00Z"
SOURCE_ID = "4ed8f56b-509e-4bde-bcea-276bd41ca638"


def seed(directory: Path, *, name: str = "source.jsonl", cwd: Path | None = None) -> Session:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    session = Session(
        source_format=AgentFormat.PI,
        source_path=path,
        source_sha256="",
        session_id=SOURCE_ID,
        cwd=cwd or directory,
        started_at=STAMP,
        cli_version=None,
        model="test-model",
        model_provider="openai",
        title="Synthetic migration",
        raw_record_count=6,
        events=(
            Event(EventKind.MESSAGE, Provenance(1), Role.USER, STAMP, "检查 café.py"),
            Event(EventKind.MESSAGE, Provenance(2), Role.ASSISTANT, STAMP, "先运行测试"),
            Event(
                EventKind.TOOL_CALL,
                Provenance(2),
                Role.ASSISTANT,
                STAMP,
                tool_name="bash",
                tool_call_id="call_1",
                payload={"input": {"command": "pytest"}},
            ),
            Event(
                EventKind.TOOL_RESULT,
                Provenance(3),
                Role.TOOL,
                STAMP,
                "2 passed",
                tool_call_id="call_1",
                payload={"is_error": False},
            ),
            Event(EventKind.MESSAGE, Provenance(4), Role.ASSISTANT, STAMP, "修好了 ✓"),
        ),
    )
    data, _ = pi.serialize(session, session_id=SOURCE_ID, cwd=session.cwd, provider="openai")
    path.write_bytes(data)
    return core.load(path, "pi")


def migrated(session: Session, target: str, directory: Path) -> Path:
    # Fixtures retain tools so each source parser is exercised before CLI filtering.
    identifier = str(uuid.uuid4())
    if target == "workbuddy":
        payload, _ = workbuddy.serialize(session, session_id=identifier, cwd=session.cwd)
        suffix = ".jsonl"
    elif target == "dsh":
        payload, _ = dsh.serialize(
            session, session_id=identifier, cwd=session.cwd, provider="openai", model="test-model"
        )
        suffix = ".jsonl.zstd"
    else:
        artifact = convert_session(
            session,
            ConversionOptions(
                TargetFormat("antigravity" if target == "antigravity-desktop" else target),
                session_id=identifier, cwd=session.cwd,
            ),
        )
        payload = artifact.native_bytes
        if target == "antigravity-desktop":
            payload = antigravity_desktop.as_desktop(
                antigravity.with_workspace(payload, session.cwd)
            )
        suffix = {
            "opencode": ".json", "antigravity": ".db", "antigravity-desktop": ".db",
        }.get(target, ".jsonl")
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{target}-{identifier}{suffix}"
    output.write_bytes(payload)
    return output


@pytest.mark.parametrize("source_format", core.FORMATS)
@pytest.mark.parametrize("target_format", core.FORMATS)
def test_conversion_matrix(tmp_path, source_format, target_format):
    source = seed(tmp_path / "seed")
    source_path = migrated(source, source_format, tmp_path / "source")
    original = source_path.read_bytes()
    parsed = core.load(source_path)
    payload, converted, manifest = core.prepare(
        parsed, target=target_format, output_dir=tmp_path / "target"
    )
    core.apply(parsed, payload, converted, manifest)
    result = core.load(converted, target_format)
    text = [event.text for event in result.events if event.kind == EventKind.MESSAGE]
    assert text == ["检查 café.py", "先运行测试", "修好了 ✓"]
    calls = [event for event in result.events if event.kind == EventKind.TOOL_CALL]
    results = [event for event in result.events if event.kind == EventKind.TOOL_RESULT]
    assert calls == results == []
    assert manifest["dropped_events"].get("filtered:tool_call", 0) == parsed.event_counts().get(
        "tool_call", 0
    )
    assert manifest["dropped_events"].get("filtered:tool_result", 0) == parsed.event_counts().get(
        "tool_result", 0
    )
    assert "2 passed" not in json.dumps(manifest, ensure_ascii=False)
    assert result.session_id != source.session_id
    assert source_path.read_bytes() == original


@pytest.mark.parametrize("target", core.FORMATS)
def test_source_model_and_provider_are_not_migrated(tmp_path, target):
    source = replace(
        seed(tmp_path / "seed"), model="PRIVATE_SOURCE_MODEL", model_provider="PRIVATE_PROVIDER"
    )
    payload, _, _ = core.prepare(source, target=target, output_dir=tmp_path / "out")
    if target == "dsh":
        from compression import zstd

        payload = zstd.decompress(payload)
    assert b"PRIVATE_SOURCE_MODEL" not in payload
    assert b"PRIVATE_PROVIDER" not in payload


def test_pi_uses_target_model_defaults_and_keeps_title(tmp_path, monkeypatch):
    home = tmp_path / "pi-home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "target-provider", "defaultModel": "target-model"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home))
    source = seed(tmp_path / "seed")
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    core.apply(source, payload, output, manifest)
    result = core.load(output)
    assert result.title == source.title
    assert (result.model_provider, result.model) == ("target-provider", "target-model")
    rows = [json.loads(line) for line in payload.splitlines()]
    assert not any(row["type"] == "thinking_level_change" for row in rows)


def test_pi_requires_destination_model_when_defaults_are_missing(tmp_path):
    source = seed(tmp_path / "seed")
    with pytest.raises(SessionMigrateError, match="--provider.*--model"):
        core.prepare(source, target="pi", target_home=tmp_path / "unconfigured-pi")
    assert source.source_path.exists()


def test_pi_explicit_model_overrides_target_defaults(tmp_path):
    source = seed(tmp_path / "seed")
    payload, output, manifest = core.prepare(
        source, target="pi", target_home=tmp_path / "no-settings",
        provider="chosen-provider", model="chosen-model",
    )
    core.apply(source, payload, output, manifest)
    result = core.load(output)
    assert (result.model_provider, result.model) == ("chosen-provider", "chosen-model")


def test_pi_project_model_defaults_override_agent_defaults(tmp_path):
    source = seed(tmp_path / "seed")
    config = source.cwd / ".pi" / "settings.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"defaultProvider": "project-provider", "defaultModel": "project-model"}),
        encoding="utf-8",
    )
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    core.apply(source, payload, output, manifest)
    result = core.load(output)
    assert (result.model_provider, result.model) == ("project-provider", "project-model")


@pytest.mark.parametrize("settings", [[], {"defaultModel": 42}])
def test_pi_rejects_invalid_target_settings(tmp_path, settings):
    home = tmp_path / "invalid-settings"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    source = seed(tmp_path / "seed")
    with pytest.raises(SessionMigrateError, match="Pi"):
        core.prepare(source, target="pi", target_home=home)


def test_copy_move_and_no_overwrite(tmp_path):
    source = seed(tmp_path / "seed")
    payload, output, manifest = core.prepare(source, target="codex", output_dir=tmp_path / "out")
    core.apply(source, payload, output, manifest)
    assert source.source_path.exists()
    with pytest.raises(SessionMigrateError, match="overwrite"):
        core.apply(source, payload, output, manifest, move=True)
    assert source.source_path.exists()
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    result = core.apply(source, payload, output, manifest, move=True)
    assert result["status"] == "moved"
    assert output.exists() and not source.source_path.exists()
    assert (
        json.loads(Path(result["manifest"]).read_text("utf-8"))["source"]["session_id"] == SOURCE_ID
    )


def test_conversation_keeps_summaries_and_user_images_only(tmp_path):
    source = seed(tmp_path / "seed")
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    source = replace(
        source,
        events=source.events
        + (
            Event(EventKind.THINKING, Provenance(5), Role.ASSISTANT, STAMP, "INTERNAL_THINKING"),
            Event(EventKind.MESSAGE, Provenance(6), Role.SYSTEM, STAMP, "INTERNAL_INSTRUCTIONS"),
            Event(EventKind.OPAQUE, Provenance(7), payload={"trace": "INTERNAL_TRACE"}),
            Event(EventKind.CONTEXT, Provenance(8), Role.SYSTEM, payload={"runtime": "PRIVATE"}),
            Event(
                EventKind.CONTEXT,
                Provenance(9),
                Role.USER,
                STAMP,
                payload={"block_type": "image", "image_url": image},
            ),
            Event(EventKind.COMPACTION, Provenance(10), Role.SYSTEM, STAMP, "portable summary"),
            Event(EventKind.MESSAGE, Provenance(11), Role.USER, STAMP, "continue"),
        ),
    )
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    core.apply(source, payload, output, manifest)
    rows = [json.loads(line) for line in payload.splitlines()]
    messages = [row["message"] for row in rows if row["type"] == "message"]
    assert all(message["role"] in {"user", "assistant"} for message in messages)
    assert any(row.get("summary") == "portable summary" for row in rows)
    assert any(
        block["type"] == "image"
        for message in messages
        if isinstance(message["content"], list)
        for block in message["content"]
    )
    assert all(
        block["type"] in {"text", "image"}
        for message in messages
        if isinstance(message["content"], list)
        for block in message["content"]
    )
    assert b"INTERNAL" not in payload and b"2 passed" not in payload
    assert manifest["source"]["events"] == source.event_counts()
    assert manifest["content"] == "conversation-only"
    assert manifest["dropped_events"] == {
        "filtered:context": 1,
        "filtered:message": 1,
        "filtered:opaque": 1,
        "filtered:session_model": 1,
        "filtered:session_model_provider": 1,
        "filtered:thinking": 1,
        "filtered:tool_call": 1,
        "filtered:tool_result": 1,
    }


def test_cli_copies_conversation_without_tools_by_default(tmp_path, capsys):
    source = seed(tmp_path / "seed")
    output = tmp_path / "pi.jsonl"
    assert (
        main(["convert", str(source.source_path), "--to", "pi", "--output", str(output), "--apply"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["status"] == "copied" and source.source_path.exists()
    assert result["dropped_events"]["filtered:tool_call"] == 1
    assert {event.kind for event in core.load(output).events} == {EventKind.MESSAGE}


def test_failed_target_read_retains_source_and_rolls_back(tmp_path, monkeypatch):
    source = seed(tmp_path / "seed")
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")

    def fail_read(*args):
        raise SessionMigrateError("simulated invalid target")

    monkeypatch.setattr(core, "load", fail_read)
    with pytest.raises(SessionMigrateError, match="simulated"):
        core.apply(source, payload, output, manifest, move=True)
    assert source.source_path.exists()
    assert not output.exists()
    assert not output.with_name(output.name + ".manifest.json").exists()


def test_changed_source_is_never_deleted(tmp_path, monkeypatch):
    source = seed(tmp_path / "seed")
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    real_load = core.load

    def read_and_change(*args):
        result = real_load(*args)
        source.source_path.write_bytes(source.source_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(core, "load", read_and_change)
    with pytest.raises(SessionMigrateError, match="source changed"):
        core.apply(source, payload, output, manifest, move=True)
    assert source.source_path.exists() and output.exists()


def test_manifest_failure_rolls_back_target(tmp_path, monkeypatch):
    source = seed(tmp_path / "seed")
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    create = core._create

    def fail_manifest(path, data):
        if path.name.endswith(".manifest.json"):
            raise OSError("simulated disk failure")
        create(path, data)

    monkeypatch.setattr(core, "_create", fail_manifest)
    with pytest.raises(OSError, match="disk failure"):
        core.apply(source, payload, output, manifest, move=True)
    assert source.source_path.exists() and not output.exists()


def test_project_batch_preview_and_apply(tmp_path, capsys):
    project = tmp_path / "project"
    child = project / "child"
    sibling = tmp_path / "project-other"
    child.mkdir(parents=True)
    sibling.mkdir()
    root = tmp_path / "codex"
    for cwd in (project, child, sibling):
        migrated(seed(tmp_path / "seed", cwd=cwd), "codex", root)
    out = tmp_path / "pi-home"
    argv = [
        "convert",
        "--from",
        "codex",
        "--to",
        "pi",
        "--source-root",
        str(root),
        "--project",
        str(project),
        "--target-home",
        str(out),
        "--provider",
        "target-provider",
        "--model",
        "target-model",
    ]
    assert main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["count"] == 2 and not out.exists()
    assert "检查" not in json.dumps(preview, ensure_ascii=False)
    assert main([*argv, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["count"] == 2
    assert len(list(out.rglob("*.jsonl"))) == 2
    assert len(list(root.glob("*.jsonl"))) == 3


def test_exact_id_selects_one_session(tmp_path, capsys):
    source = seed(tmp_path / "seed")
    root = tmp_path / "codex"
    first = migrated(source, "codex", root)
    migrated(source, "codex", root)
    identifier = core.load(first).session_id
    assert (
        main(
            [
                "convert",
                "--from",
                "codex",
                "--to",
                "pi",
                "--source-root",
                str(root),
                "--session",
                identifier,
                "--output-dir",
                str(tmp_path / "out"),
                "--apply",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_invalid_input_and_partial_batch_are_reported(tmp_path, capsys):
    root = tmp_path / "pi"
    seed(root)
    (root / "broken.jsonl").write_text("not json\n", encoding="utf-8")
    assert (
        main(
            [
                "convert",
                str(root),
                "--from",
                "pi",
                "--to",
                "codex",
                "--output-dir",
                str(tmp_path / "out"),
                "--apply",
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["count"] == 1 and len(result["errors"]) == 1
    assert (root / "broken.jsonl").exists()


def test_dsh_replacement_and_future_version(tmp_path):
    path = migrated(seed(tmp_path / "seed"), "dsh", tmp_path / "dsh")
    rows, _ = dsh.read_records(path)
    rows.append(
        {
            "type": "user/message",
            "seq": len(rows) - 1,
            "time": rows[0]["createdAt"],
            "surfaceOp": {"op": "replace", "startSeq": 1, "endSeq": 1},
            "sourceEventSeqs": [1],
            "data": {
                "id": "replacement",
                "role": "user",
                "source": {"kind": "user"},
                "content": [{"type": "text", "text": "summary"}],
            },
        }
    )
    plain = tmp_path / "replaced.jsonl"
    plain.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = core.load(plain)
    assert [event.text for event in result.events if event.kind == EventKind.MESSAGE][
        0
    ] == "summary"
    rows[0]["version"] = 999
    plain.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    with pytest.raises(SessionMigrateError, match="v4"):
        core.load(plain)


def test_truncated_dsh_and_required_unknown_event_fail(tmp_path):
    path = migrated(seed(tmp_path / "seed"), "dsh", tmp_path / "dsh")
    data = path.read_bytes()
    path.write_bytes(data[:-2])
    with pytest.raises(SessionMigrateError, match="truncated"):
        core.load(path)
    path.write_bytes(data)
    rows, _ = dsh.read_records(path)
    rows.append(
        {"seq": len(rows) - 1, "time": rows[0]["createdAt"], "type": "unknown/required", "data": {}}
    )
    plain = tmp_path / "unknown.jsonl"
    plain.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(SessionMigrateError, match="unsupported required"):
        core.load(plain)


def test_dsh_multiple_generations_refuse_move(tmp_path):
    path = migrated(seed(tmp_path / "seed"), "dsh", tmp_path / "dsh")
    (path.parent / "session.v3.jsonl.zstd").write_bytes(b"older generation")
    source = core.load(path)
    payload, output, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    with pytest.raises(SessionMigrateError, match="multiple generations"):
        core.apply(source, payload, output, manifest, move=True)
    assert source.source_path.exists() and not output.exists()


def test_source_hash_is_over_original_compressed_bytes(tmp_path):
    path = migrated(seed(tmp_path / "seed"), "dsh", tmp_path / "dsh")
    assert core.load(path).source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_unanswered_tool_calls_are_filtered_before_dsh_export(tmp_path):
    source = seed(tmp_path / "seed")
    incomplete = replace(source, events=source.events[:3])
    _, _, manifest = core.prepare(incomplete, target="dsh", output_dir=tmp_path / "out")
    assert manifest["dropped_events"]["filtered:tool_call"] == 1


def test_no_matches_returns_error(tmp_path, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    assert main(["convert", str(root), "--from", "pi", "--to", "codex"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"] == [{"error": "no matching sessions"}]


def test_project_key_utf16_matches_native_algorithm():
    assert dsh.project_key(Path("D:/work/😀")) == "--D-work-~D83D~DE00--"


def test_list_source_root_and_opencode_manifest_filter(tmp_path, capsys):
    root = tmp_path / "exports"
    migrated(seed(tmp_path / "seed"), "opencode", root)
    assert main(["list", "--from", "opencode", "--source-root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_duplicate_exact_id_refuses_move(tmp_path, capsys):
    root = tmp_path / "pi"
    first = seed(root, name="first.jsonl")
    second = seed(root, name="second.jsonl")
    assert (
        main(
            [
                "convert",
                "--from",
                "pi",
                "--source-root",
                str(root),
                "--session",
                SOURCE_ID,
                "--to",
                "codex",
                "--move",
                "--apply",
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
        == 1
    )
    assert "ambiguous" in json.loads(capsys.readouterr().out)["error"]
    assert first.source_path.exists() and second.source_path.exists()
    assert not (tmp_path / "out").exists()


def test_cli_single_id_output_and_move(tmp_path, capsys):
    root = tmp_path / "codex"
    source = seed(tmp_path / "seed")
    chosen = migrated(source, "codex", root)
    other = migrated(source, "codex", root)
    identifier = core.load(chosen).session_id
    output = tmp_path / "chosen-pi.jsonl"
    assert (
        main(
            [
                "convert",
                "--from",
                "codex",
                "--source-root",
                str(root),
                "--session",
                identifier,
                "--to",
                "pi",
                "--output",
                str(output),
                "--move",
                "--apply",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "moved"
    assert output.exists() and not chosen.exists() and other.exists()


def test_corrupt_zstd_is_a_json_error(tmp_path, capsys):
    path = tmp_path / "broken.jsonl.zstd"
    path.write_bytes(b"not a zstd stream")
    assert main(["inspect", str(path)]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_dsh_physical_source_ranges(tmp_path):
    path = migrated(seed(tmp_path / "seed"), "dsh", tmp_path / "dsh")
    rows, _ = dsh.read_records(path)
    first = next(row["seq"] for row in rows[1:] if row.get("type") == "user/message")
    last = max(row["seq"] for row in rows[1:] if row.get("surfaceOp") == "append")

    def append(kind, data, **metadata):
        rows.append(
            {
                "type": kind,
                "seq": len(rows) - 1,
                "time": rows[0]["createdAt"],
                "data": data,
                **metadata,
            }
        )

    append("turn/start", {"turn": 2})
    append(
        "user/message",
        {
            "id": "compacted-user",
            "role": "user",
            "source": {"kind": "user"},
            "content": [{"type": "text", "text": "only the current summary"}],
        },
        surfaceOp={"op": "replace", "startSeq": first, "endSeq": last},
        sourceEventSeqs=[[first, last]],
    )
    append("turn/end", {"turn": 2, "reason": {"kind": "completed"}})
    plain = tmp_path / "ranges.jsonl"
    plain.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = core.load(plain)
    assert [event.text for event in result.events if event.kind == EventKind.MESSAGE] == [
        "only the current summary"
    ]


def test_dsh_decompression_limit(tmp_path, monkeypatch):
    path = migrated(seed(tmp_path / "seed"), "dsh", tmp_path / "dsh")
    compressed_size = path.stat().st_size
    monkeypatch.setattr(dsh, "MAX_BYTES", compressed_size + 10)
    with pytest.raises(SessionMigrateError, match="expanded DSH input"):
        dsh.read_records(path)


def test_dsh_summary_replaces_active_context_and_keeps_physical_history(tmp_path):
    source = seed(tmp_path / "seed")
    compacted = replace(
        source,
        events=source.events
        + (
            Event(EventKind.COMPACTION, Provenance(5), Role.USER, STAMP, "important summary"),
            Event(EventKind.MESSAGE, Provenance(6), Role.USER, STAMP, "continue"),
        ),
    )
    path = migrated(compacted, "dsh", tmp_path / "out")
    rows, _ = dsh.read_records(path)
    assert any(
        row.get("data", {}).get("role") == "user" and "检查" in json.dumps(row, ensure_ascii=False)
        for row in rows
    )
    result = core.load(path)
    assert [event.text for event in result.events if event.kind == EventKind.MESSAGE] == [
        "[Migrated compaction summary]\nimportant summary",
        "continue",
    ]


def test_dsh_invalid_tool_source_is_rejected(tmp_path):
    path = migrated(seed(tmp_path / "seed"), "dsh", tmp_path / "out")
    rows, _ = dsh.read_records(path)
    tool = next(row for row in rows if row.get("type") == "tool/result")
    tool["data"]["message"]["source"]["callId"] = "wrong-call"
    plain = tmp_path / "invalid-tool.jsonl"
    plain.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(SessionMigrateError, match="identity"):
        core.load(plain)
