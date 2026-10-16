"""Desktop copy/cut preserves neighbor sessions and both SQLite history indexes."""

import base64
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from session_migrate.errors import SessionMigrateError
from session_migrate.model import Event, EventKind, Provenance, Role
from test_bridge import seed

from agent_session_bridge import antigravity, core, stores
from agent_session_bridge import antigravity_desktop as desktop
from agent_session_bridge.cli import main


def desktop_home(root):
    home = root / ".gemini" / "antigravity-ide"
    (home / "conversations").mkdir(parents=True)
    (home / "installation_id").write_text("isolated-test-installation")
    state = desktop.state_path(home)
    state.parent.mkdir(parents=True)
    with closing(sqlite3.connect(state)) as c, c:
        c.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        c.execute("INSERT INTO ItemTable VALUES ('unrelated.settings', 'keep-exactly')")
    for name in desktop.CACHES:
        (home / name).write_bytes(b"")
    return home, state


def imported(source, home, **options):
    payload, path, manifest = core.prepare(
        source, target="antigravity-desktop", target_home=home, **options
    )
    core.apply(source, payload, path, manifest)
    return core.load(path)


def state_indexes(path):
    with closing(sqlite3.connect(path)) as c:
        return desktop.indexes(c)


def test_desktop_portable_title_cwd_summaries_and_model_filter(tmp_path):
    source = replace(seed(tmp_path / "seed"), title='原标题 "迁移" ✓')
    summary = Event(EventKind.COMPACTION, Provenance(9), Role.SYSTEM, text="Earlier context")
    source = replace(source, events=(*source.events, summary))
    payload, path, manifest = core.prepare(
        source, target="antigravity-desktop", output_dir=tmp_path / "portable"
    )
    assert source.title not in json.dumps(manifest)
    assert "Earlier context" not in json.dumps(manifest)
    assert b"test-model" not in payload
    core.apply(source, payload, path, manifest)
    parsed = core.load(path)
    assert parsed.source_format.value == "antigravity-desktop"
    assert parsed.title == source.title and parsed.cwd == source.cwd
    assert parsed.model is parsed.model_provider is None
    assert any(e.text == "[Imported conversation summary]\nEarlier context" for e in parsed.events)
    assert not any(e.kind == EventKind.TOOL_CALL for e in parsed.events)
    assert path in core.source_paths("antigravity-desktop", path.parent)


def test_desktop_structured_user_text_and_edited_assistant_response(tmp_path):
    native = antigravity.native
    source = seed(tmp_path / "seed")
    payload, path, manifest = core.prepare(
        source, target="antigravity-desktop", output_dir=tmp_path / "portable"
    )
    core.apply(source, payload, path, manifest)
    with closing(sqlite3.connect(path)) as c, c:
        for index, kind in c.execute("SELECT idx,step_type FROM steps"):
            if kind == native.STEP_TYPE_USER_INPUT:
                nested = (native._field_bytes(3, native._field_text(1, "检查 "))
                          + native._field_bytes(3, native._field_bytes(2, b""))
                          + native._field_bytes(3, native._field_text(1, "café.py")))
                field = 19
            else:
                nested = native._field_text(1, "old response") + native._field_text(8, "编辑 ✓")
                field = 20
            outer = (native._field_varint(1, kind) + native._field_varint(4, 3)
                     + native._field_bytes(field, nested))
            c.execute("UPDATE steps SET step_payload=? WHERE idx=?", (outer, index))
    before = path.read_bytes()
    parsed = core.load(path)
    messages = [e.text for e in parsed.events if e.kind == EventKind.MESSAGE]
    assert messages == ["检查 café.py", "编辑 ✓", "编辑 ✓"]
    assert path.read_bytes() == before


def test_native_project_selection_and_cut_clean_both_indexes(tmp_path, capsys):
    home, state = desktop_home(tmp_path / "profile")
    source = imported(seed(tmp_path / "seed"), home)
    neighbor = imported(seed(tmp_path / "neighbor"), home)
    assert source.cwd == tmp_path / "seed" and source.title == "Synthetic migration"
    with closing(sqlite3.connect(source.source_path)) as c:
        assert c.execute("SELECT source FROM trajectory_meta").fetchone() == (1,)
    assert main(["list", "--from", "antigravity-desktop", "--source-root", str(home),
                 "--project", str(source.cwd), "--source-state-db", str(state)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["count"] == 1 and source.title not in json.dumps(result)
    assert main(["convert", "--from", "antigravity-desktop", "--source-root", str(home),
                 "--session", source.session_id, "--source-state-db", str(state),
                 "--to", "pi", "--output-dir", str(tmp_path / "out"), "--move", "--apply"]) == 0
    target = json.loads(capsys.readouterr().out)["results"][0]
    assert core.load(Path(target["target"])).title == source.title
    assert not source.source_path.exists()
    assert not antigravity.annotation_path(home, source.session_id).exists()
    assert not source.source_path.with_name(source.source_path.name + ".manifest.json").exists()
    raw, index = state_indexes(state)
    assert set(index["entries"]) == {neighbor.session_id}
    assert source.session_id.encode() not in raw and neighbor.session_id.encode() in raw
    for name in desktop.CACHES:
        raw = (home / name).read_bytes()
        assert source.session_id.encode() not in raw and neighbor.session_id.encode() in raw
    with closing(sqlite3.connect(state)) as c:
        value = c.execute("SELECT value FROM ItemTable WHERE key='unrelated.settings'").fetchone()
        assert value == (
            "keep-exactly",
        )
    assert neighbor.source_path.exists()


def test_explicit_desktop_state_db_and_unsupported_schema(tmp_path, capsys):
    home, state = desktop_home(tmp_path / "profile")
    custom_home = tmp_path / "custom-data"
    home.rename(custom_home)
    source = seed(tmp_path / "seed")
    with pytest.raises(SessionMigrateError, match="state.vscdb missing"):
        core.prepare(source, target="antigravity-desktop", target_home=custom_home)
    assert main(["convert", str(source.source_path), "--to", "antigravity-desktop",
                 "--target-home", str(custom_home), "--target-state-db", str(state),
                 "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)["results"][0]
    parsed = core.load(Path(result["target"]), state_database=state)
    stores.remove_source(parsed)
    assert not parsed.source_path.exists()
    with closing(sqlite3.connect(state)) as c, c:
        c.execute("DROP TABLE ItemTable")
    with pytest.raises(SessionMigrateError, match="schema"):
        core.prepare(source, target="antigravity-desktop", target_home=custom_home,
                     target_state_db=state)


def test_desktop_cut_rolls_back_files_caches_and_database(tmp_path, monkeypatch):
    home, state = desktop_home(tmp_path / "profile")
    source = imported(seed(tmp_path / "seed"), home)
    before = {name: (home / name).read_bytes() for name in desktop.CACHES}
    original_indexes = state_indexes(state)
    original_replace = stores._replace

    def fail_second_cache(path, data):
        if path.name == desktop.CACHES[1]:
            raise OSError("cache denied")
        return original_replace(path, data)

    monkeypatch.setattr(stores, "_replace", fail_second_cache)
    with pytest.raises(OSError, match="cache denied"):
        stores.remove_source(source)
    assert source.source_path.exists()
    assert antigravity.annotation_path(home, source.session_id).exists()
    assert state_indexes(state) == original_indexes
    assert all((home / name).read_bytes() == value for name, value in before.items())


def test_desktop_target_registration_failure_retains_source(tmp_path, monkeypatch):
    home, state = desktop_home(tmp_path / "profile")
    source = seed(tmp_path / "seed")
    payload, path, manifest = core.prepare(source, target="antigravity-desktop", target_home=home)

    def fail(*args):
        raise OSError("cache denied")

    monkeypatch.setattr(stores, "_replace", fail)
    with pytest.raises(OSError, match="cache denied"):
        core.apply(source, payload, path, manifest, move=True)
    assert source.source_path.exists() and not path.exists()
    assert not antigravity.annotation_path(home, manifest["target"]["session_id"]).exists()
    assert state_indexes(state)[1]["entries"] == {}


@pytest.mark.parametrize("blocker", ["legacy", "wal", "daemon", "future-index", "changed-source"])
def test_desktop_move_rejects_unsafe_source_without_deleting(tmp_path, blocker):
    home, state = desktop_home(tmp_path / "profile")
    source = imported(seed(tmp_path / "seed"), home)
    if blocker == "legacy":
        source.source_path.with_suffix(".pb").write_bytes(b"legacy")
    elif blocker == "wal":
        Path(str(source.source_path) + "-journal").touch()
    elif blocker == "daemon":
        (home / "daemon").mkdir()
        (home / "daemon" / "ls.json").write_text(json.dumps({"pid": os.getpid()}))
    elif blocker == "future-index":
        with closing(sqlite3.connect(state)) as c, c:
            c.execute("UPDATE ItemTable SET value=? WHERE key=?", (
                json.dumps({"version": 99, "entries": {}}), desktop.JSON_KEY,
            ))
    else:
        with closing(sqlite3.connect(source.source_path)) as c, c:
            c.execute("UPDATE steps SET status=7 WHERE idx=0")
    payload, path, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    with pytest.raises(SessionMigrateError):
        core.apply(source, payload, path, manifest, move=True)
    assert source.source_path.exists()


def test_desktop_state_metadata_and_legacy_pb_errors(tmp_path):
    home, state = desktop_home(tmp_path / "profile")
    source = imported(seed(tmp_path / "seed"), home)
    antigravity.annotation_path(home, source.session_id).unlink()
    source.source_path.with_name(source.source_path.name + ".manifest.json").unlink()
    assert core.load(source.source_path).title == source.title
    raw, _ = state_indexes(state)
    assert raw and base64.b64encode(raw)
    pb = tmp_path / "legacy.pb"
    pb.write_bytes(b"legacy")
    with pytest.raises(SessionMigrateError, match="official desktop client"):
        core.load(pb, "antigravity-desktop")
