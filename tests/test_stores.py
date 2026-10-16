"""Focused persistence tests; native integration uses private real conversations separately."""

import json
import sqlite3

import pytest
from session_migrate.errors import SessionMigrateError
from test_bridge import migrated, seed

from agent_session_bridge import core, stores


def codex_db(home):
    home.mkdir(parents=True, exist_ok=True)
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as c:
        c.executescript("""
          CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
            cwd TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
            model_provider TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
          CREATE TABLE thread_attachments(id TEXT PRIMARY KEY, thread_id TEXT
            REFERENCES threads(id) ON DELETE CASCADE);
          CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);
        """)
    return db


def native_codex(tmp_path):
    home = tmp_path / "home"
    db = codex_db(home)
    source = seed(tmp_path / "seed")
    payload, path, manifest = core.prepare(source, target="codex", target_home=home)
    core.apply(source, payload, path, manifest)
    return home, db, core.load(path, "codex")


def test_native_codex_register_cut_removes_rows_and_cache_keeps_neighbor(tmp_path):
    home, db, source = native_codex(tmp_path)
    sid = source.session_id
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT * FROM threads WHERE id=?", (sid,)).fetchone()
        c.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)", ("neighbor", *row[1:]))
        c.execute("INSERT INTO thread_attachments VALUES (?,?)", ("attachment", sid))
        c.execute("INSERT INTO thread_spawn_edges VALUES (?,?)", ("neighbor", sid))
    index = home / "session_index.jsonl"
    neighbor = b'{"id":"neighbor","thread_name":"keep"}\n'
    index.write_bytes(json.dumps({"id": sid}).encode() + b"\n" + neighbor)
    payload, path, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    core.apply(source, payload, path, manifest, move=True)
    assert not source.source_path.exists()
    assert index.read_bytes() == neighbor
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT id FROM threads").fetchall() == [("neighbor",)]
        assert c.execute("SELECT * FROM thread_attachments").fetchall() == []
        assert c.execute("SELECT * FROM thread_spawn_edges").fetchall() == []


def test_database_delete_failure_rolls_back_source_and_index(tmp_path, monkeypatch):
    home, db, source = native_codex(tmp_path)
    original = source.source_path.read_bytes()
    index = home / "session_index.jsonl"
    index.write_text(json.dumps({"id": source.session_id}) + "\n")
    old_index = index.read_bytes()

    def fail(*args):
        raise OSError("index write denied")

    monkeypatch.setattr(stores, "_replace", fail)
    with pytest.raises(OSError, match="denied"):
        stores.remove_source(source)
    assert source.source_path.read_bytes() == original
    assert index.read_bytes() == old_index
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT id FROM threads").fetchone()[0] == source.session_id


def test_unknown_required_target_column_keeps_source_and_cleans_target(tmp_path):
    home = tmp_path / "home"
    db = codex_db(home)
    with sqlite3.connect(db) as c:
        c.execute("ALTER TABLE threads ADD COLUMN unknown_required TEXT NOT NULL")
    source = seed(tmp_path / "seed")
    payload, path, manifest = core.prepare(source, target="codex", target_home=home)
    with pytest.raises(SessionMigrateError, match="unknown_required"):
        core.apply(source, payload, path, manifest, move=True)
    assert source.source_path.exists() and not path.exists()
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM threads").fetchone()[0] == 0


def test_workbuddy_native_register_cut_cleans_db_and_recent_cache(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with sqlite3.connect(home / "workbuddy.db") as c:
        c.executescript("""
          CREATE TABLE sessions(id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL,
            title TEXT, status TEXT, created_at INTEGER, updated_at INTEGER, deleted_at INTEGER);
          CREATE TABLE workspaces(path TEXT PRIMARY KEY, last_opened_at INTEGER NOT NULL);
          CREATE TABLE session_usage(session_id TEXT PRIMARY KEY, used INTEGER);
        """)
    source = seed(tmp_path / "seed")
    payload, path, manifest = core.prepare(source, target="workbuddy", target_home=home)
    core.apply(source, payload, path, manifest)
    sid = manifest["target"]["session_id"]
    with sqlite3.connect(home / "workbuddy.db") as c:
        c.execute("INSERT INTO session_usage VALUES (?,?)", (sid, 12))
    (home / "app").mkdir()
    cache = home / "app" / "sessions.json"
    cache.write_text(
        json.dumps(
            {"version": 1, "sessions": [{"conversationId": sid}, {"conversationId": "neighbor"}]}
        )
    )
    stores.remove_source(core.load(path, "workbuddy"))
    with sqlite3.connect(home / "workbuddy.db") as c:
        assert c.execute("SELECT * FROM sessions").fetchall() == []
        assert c.execute("SELECT * FROM session_usage").fetchall() == []
        assert c.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
    assert json.loads(cache.read_text())["sessions"] == [{"conversationId": "neighbor"}]


def test_pi_runtime_roles_do_not_block_dialogue(tmp_path):
    path = migrated(seed(tmp_path / "seed"), "pi", tmp_path / "input")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1:1] = [
        {
            "type": "message",
            "id": "extra",
            "parentId": None,
            "timestamp": rows[0]["timestamp"],
            "message": {"role": "system", "content": "PRIVATE_SYSTEM"},
        }
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    source = core.load(path)
    payload, _, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    assert b"PRIVATE_SYSTEM" not in payload
    assert manifest["dropped_events"]["filtered:opaque"] >= 1


def test_opencode_native_cut_removes_parts_and_export_preserves_neighbor(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = home / "opencode.db"
    with sqlite3.connect(db) as c:
        c.executescript("""
          CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT, sandboxes TEXT,
            time_created INTEGER, time_updated INTEGER);
          CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, slug TEXT, directory TEXT,
            title TEXT, version TEXT, time_created INTEGER, time_updated INTEGER);
          CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT REFERENCES session(id)
            ON DELETE CASCADE, data TEXT, time_created INTEGER, time_updated INTEGER);
          CREATE TABLE part(id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT
            REFERENCES message(id) ON DELETE CASCADE, data TEXT,
            time_created INTEGER, time_updated INTEGER);
        """)
    source = seed(tmp_path / "seed")
    identifiers = []
    for _ in range(2):
        payload, path, manifest = core.prepare(source, target="opencode", target_home=home)
        core.apply(source, payload, path, manifest)
        identifiers.append(manifest["target"]["session_id"])
    selected = next(p for p in core.source_paths("opencode", home) if p.name == identifiers[0])
    native_source = core.load(selected, "opencode")
    payload, path, manifest = core.prepare(native_source, target="pi", output_dir=tmp_path / "out")
    core.apply(native_source, payload, path, manifest, move=True)
    assert [p.name for p in core.source_paths("opencode", home)] == [identifiers[1]]
    assert not (home / "asb-imports" / f"{identifiers[0]}.json").exists()
    with sqlite3.connect(db) as c:
        for table in ("message", "part"):
            assert (
                c.execute(
                    f"SELECT count(*) FROM {table} WHERE session_id=?", (identifiers[0],)
                ).fetchone()[0]
                == 0
            )


def test_opencode_rejects_unknown_schema_and_unindexed_git_project(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = home / "opencode.db"
    with sqlite3.connect(db) as c:
        c.executescript("""
          CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT);
          CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT);
          CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
          CREATE TABLE part(id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, data TEXT);
        """)
    source = seed(tmp_path / "seed")
    project = tmp_path / "new-project"
    project.mkdir()
    (project / ".git").mkdir()
    payload, path, manifest = core.prepare(source, target="opencode", target_home=home, cwd=project)
    with pytest.raises(SessionMigrateError, match="project is not indexed"):
        core.apply(source, payload, path, manifest, move=True)
    assert source.source_path.exists() and not path.exists()
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE session_v2(id TEXT)")
    with pytest.raises(SessionMigrateError, match="session_v2"):
        core.source_paths("opencode", home)


def test_bridge_dsh_manifest_is_not_an_older_generation(tmp_path):
    source = seed(tmp_path / "seed")
    payload, path, manifest = core.prepare(source, target="dsh", target_home=tmp_path / "dsh")
    core.apply(source, payload, path, manifest)
    converted = core.load(path, "dsh")
    payload, out, manifest = core.prepare(converted, target="pi", output_dir=tmp_path / "out")
    core.apply(converted, payload, out, manifest, move=True)
    assert out.exists() and not path.exists()
