"""Antigravity SQLite indexes, portable metadata and verified cuts."""

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
from agent_session_bridge.cli import main


def summary_db(home):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "conversation_summaries.db"
    with closing(sqlite3.connect(path)) as c, c:
        c.executescript('''
          CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '', step_count INTEGER NOT NULL DEFAULT 0,
            last_modified_time DATETIME NOT NULL, workspace_uris TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '', agent_name TEXT NOT NULL DEFAULT '',
            parent_conversation_id TEXT NOT NULL DEFAULT '',
            nesting_depth INTEGER NOT NULL DEFAULT 0,
            battle_id TEXT NOT NULL DEFAULT '', winning_conversation_id TEXT NOT NULL DEFAULT '',
            not_fully_idle NUMERIC NOT NULL DEFAULT false, killed NUMERIC NOT NULL DEFAULT false,
            last_user_input_time DATETIME NOT NULL,
            last_user_input_step_index INTEGER NOT NULL DEFAULT -1,
            app_data_dir TEXT NOT NULL DEFAULT '', raw_summary BLOB,
            group_id TEXT NOT NULL DEFAULT ''
          );
          CREATE INDEX idx_conversation_summaries_last_user_input_time
            ON conversation_summaries(last_user_input_time);
          CREATE INDEX idx_conversation_summaries_last_modified_time
            ON conversation_summaries(last_modified_time);
        ''')
    return path


def native_session(tmp_path):
    home = tmp_path / "agy"
    db = summary_db(home)
    source = seed(tmp_path / "seed")
    payload, path, manifest = core.prepare(source, target="antigravity", target_home=home)
    core.apply(source, payload, path, manifest)
    return home, db, core.load(path)


def test_portable_title_cwd_summaries_and_content_free_preview(tmp_path):
    source = seed(tmp_path / "seed")
    summary = Event(EventKind.COMPACTION, Provenance(5), Role.SYSTEM, text="Earlier context")
    source = replace(source, events=(*source.events, summary))
    payload, path, manifest = core.prepare(
        source, target="antigravity", output_dir=tmp_path / "export"
    )
    assert source.title not in json.dumps(manifest)
    assert "Earlier context" not in json.dumps(manifest)
    core.apply(source, payload, path, manifest)
    parsed = core.load(path)
    assert path.suffix == ".db"
    assert parsed.title == source.title and parsed.cwd == source.cwd
    assert any("Earlier context" in (e.text or "") for e in parsed.events)
    assert manifest["dropped_events"]["compaction:converted_to_user_context"] == 1
    assert not any(e.kind == EventKind.TOOL_CALL for e in parsed.events)


def test_native_title_project_selection_and_exact_cut(tmp_path, capsys):
    home, db, source = native_session(tmp_path)
    original = source.source_path.read_bytes()
    annotation = antigravity.annotation_path(home, source.session_id)
    assert source.title in annotation.read_text(encoding="utf-8")
    with closing(sqlite3.connect(source.source_path)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    assert source.title == "Synthetic migration"
    assert source.cwd == tmp_path / "seed"
    neighbor_source = seed(tmp_path / "neighbor")
    payload, neighbor, manifest = core.prepare(
        neighbor_source, target="antigravity", target_home=home
    )
    core.apply(neighbor_source, payload, neighbor, manifest)
    neighbor_id = manifest["target"]["session_id"]
    assert main(["list", "--from", "antigravity", "--source-root", str(home),
                 "--project", str(source.cwd)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    cache = home / "cache" / "last_conversations.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({str(source.cwd): source.session_id, "other": neighbor_id}))
    assert main(["convert", "--from", "antigravity", "--source-root", str(home),
                 "--session", source.session_id, "--to", "pi",
                 "--output-dir", str(tmp_path / "out"), "--move", "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert core.load(Path(result["target"])).title == source.title
    assert original and not source.source_path.exists()
    assert not annotation.exists()
    assert not source.source_path.with_name(source.source_path.name + ".manifest.json").exists()
    assert neighbor.exists()
    assert antigravity.annotation_path(home, neighbor_id).exists()
    cache_bytes = (home / "jetbox_summaries_proto.pb").read_bytes()
    assert source.session_id.encode() not in cache_bytes and neighbor_id.encode() in cache_bytes
    assert json.loads(cache.read_text()) == {"other": neighbor_id}
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("SELECT conversation_id FROM conversation_summaries").fetchall() == [
            (neighbor_id,)
        ]


def test_native_requires_initialized_compatible_index(tmp_path):
    source = seed(tmp_path / "seed")
    with pytest.raises(SessionMigrateError, match="DB missing"):
        core.prepare(source, target="antigravity", target_home=tmp_path / "missing")
    home = tmp_path / "bad"
    db = summary_db(home)
    with closing(sqlite3.connect(db)) as c, c:
        c.execute("ALTER TABLE conversation_summaries ADD COLUMN required_future TEXT NOT NULL")
    payload, path, manifest = core.prepare(source, target="antigravity", target_home=home)
    with pytest.raises(SessionMigrateError, match="required_future"):
        core.apply(source, payload, path, manifest, move=True)
    assert source.source_path.exists() and not path.exists()


def test_cut_rolls_back_index_and_files_if_cache_update_fails(tmp_path, monkeypatch):
    home, db, source = native_session(tmp_path)
    cache = home / "cache" / "last_conversations.json"
    cache.parent.mkdir()
    original = json.dumps({"project": source.session_id}).encode()
    cache.write_bytes(original)

    def fail(*args):
        raise OSError("cache denied")

    monkeypatch.setattr(stores, "_replace", fail)
    with pytest.raises(OSError, match="cache denied"):
        stores.remove_source(source)
    assert source.source_path.exists() and cache.read_bytes() == original
    assert antigravity.annotation_path(home, source.session_id).exists()
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("SELECT conversation_id FROM conversation_summaries").fetchone() == (
            source.session_id,
        )


def test_live_wal_can_be_copied_but_cannot_be_cut(tmp_path):
    _, _, source = native_session(tmp_path)
    with closing(sqlite3.connect(source.source_path)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("UPDATE steps SET status=status")
        c.commit()
        live = core.load(source.source_path)
        payload, path, manifest = core.prepare(live, target="pi", output_dir=tmp_path / "copy")
        core.apply(live, payload, path, manifest)
        assert core.load(path).title == source.title
        payload, path, manifest = core.prepare(live, target="pi", output_dir=tmp_path / "cut")
        with pytest.raises(SessionMigrateError, match="close.*Antigravity"):
            core.apply(live, payload, path, manifest, move=True)
        assert source.source_path.exists() and not path.exists()


def test_changed_conversation_is_retained_on_cut(tmp_path):
    _, _, source = native_session(tmp_path)
    with closing(sqlite3.connect(source.source_path)) as c, c:
        c.execute("UPDATE steps SET status=7 WHERE idx=0")
    with pytest.raises(SessionMigrateError, match="source changed"):
        stores.remove_source(source)
    assert source.source_path.exists()


def test_missing_index_blocks_native_cut_and_pb_has_clear_error(tmp_path):
    _, db, source = native_session(tmp_path)
    db.unlink()
    with pytest.raises(SessionMigrateError, match="DB missing"):
        core._source_can_move(source)
    pb = tmp_path / "desktop.pb"
    pb.write_bytes(b"unsupported")
    with pytest.raises(SessionMigrateError, match="desktop.*resume"):
        core.load(pb, "antigravity")


def test_target_cache_failure_rolls_back_files_and_index(tmp_path, monkeypatch):
    home = tmp_path / "agy"
    db = summary_db(home)
    source = seed(tmp_path / "seed")
    payload, path, manifest = core.prepare(source, target="antigravity", target_home=home)

    def fail(*args):
        raise OSError("cache denied")

    monkeypatch.setattr(stores, "_replace", fail)
    with pytest.raises(OSError, match="cache denied"):
        core.apply(source, payload, path, manifest, move=True)
    assert source.source_path.exists() and not path.exists()
    assert not antigravity.annotation_path(home, manifest["target"]["session_id"]).exists()
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("SELECT count(*) FROM conversation_summaries").fetchone() == (0,)


def test_native_generated_title_and_empty_workspace(tmp_path):
    _, db, source = native_session(tmp_path)
    with closing(sqlite3.connect(db)) as c, c:
        c.execute(
            "UPDATE conversation_summaries SET title='',preview=?,workspace_uris=''",
            ("Native generated title",),
        )
    parsed = core.load(source.source_path)
    assert parsed.title == "Native generated title" and parsed.cwd is None


def test_active_presence_lock_blocks_native_writes(tmp_path):
    home = tmp_path / "agy"
    presence = home / "presence"
    presence.mkdir(parents=True)
    lock = presence / "conversation.lock"
    lock.touch()
    with lock.open("r+b") as stream:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SessionMigrateError, match="close.*Antigravity"):
            antigravity.ensure_store_closed(home)
    antigravity.ensure_store_closed(home)
