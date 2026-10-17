"""Synthetic SQLite fixtures; real-session validation stays outside the repository."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from session_migrate.errors import SessionMigrateError
from session_migrate.model import Event, EventKind, Provenance, Role
from test_bridge import STAMP, seed

from agent_session_bridge import core, stores
from agent_session_bridge.cli import main


def native_home(root: Path, *, desktop: bool = True) -> Path:
    db = root / "cli" / "db" / "db.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.executescript("""
          CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_id TEXT,
            parent_id TEXT, slug TEXT NOT NULL, directory TEXT NOT NULL, path TEXT,
            title TEXT NOT NULL, version TEXT NOT NULL, revert TEXT,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'interactive',
            title_source TEXT NOT NULL DEFAULT 'first_input', time_title_updated INTEGER);
          CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT NOT NULL
            REFERENCES session(id) ON DELETE CASCADE, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL, sequence INTEGER);
          CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT NOT NULL
            REFERENCES message(id) ON DELETE CASCADE, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL, sequence INTEGER);
          CREATE TABLE session_entry(id TEXT PRIMARY KEY, session_id TEXT NOT NULL
            REFERENCES session(id) ON DELETE CASCADE, type TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
          CREATE TABLE input_history(id TEXT PRIMARY KEY, session_id TEXT, text TEXT);
          CREATE TABLE session_input(id TEXT PRIMARY KEY, session_id TEXT, status TEXT);
          CREATE TABLE local_setting(scope TEXT, scope_id TEXT, value TEXT);
          CREATE TABLE model_usage(id TEXT PRIMARY KEY, session_id TEXT, status TEXT);
          CREATE TABLE dwf_actor(id INTEGER PRIMARY KEY, session_id TEXT);
        """)
    if desktop:
        index = root / "v2" / "tasks-index.sqlite"
        index.parent.mkdir()
        with sqlite3.connect(index) as c:
            c.executescript("""
              CREATE TABLE tasks(workspace_key TEXT NOT NULL, workspace_path TEXT NOT NULL,
                workspace_identity TEXT, task_id TEXT NOT NULL, title TEXT NOT NULL,
                provider TEXT, model TEXT, mode TEXT, task_status TEXT,
                title_overridden INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                meta_json TEXT NOT NULL, PRIMARY KEY(workspace_key,task_id));
              CREATE TABLE task_group_members(workspace_key TEXT, task_id TEXT, group_id TEXT);
              CREATE TABLE task_group_view_node_orders(node_type TEXT, node_key TEXT);
              CREATE TABLE task_groups(group_id TEXT PRIMARY KEY);
              CREATE TABLE automations(id TEXT PRIMARY KEY, target_task_id TEXT);
            """)
    return root


def imported(source, home):
    payload, path, manifest = core.prepare(source, target="zcode", target_home=home)
    core.apply(source, payload, path, manifest)
    virtual = Path(manifest["target"]["store"]["database"]) / manifest["target"]["session_id"]
    return core.load(virtual), path


def texts(session):
    return [e.text for e in session.events if e.kind in {EventKind.MESSAGE, EventKind.COMPACTION}]


def test_native_copy_and_cut_clean_both_databases_keep_neighbor(tmp_path):
    home = native_home(tmp_path / "zcode")
    original = seed(tmp_path / "source")
    selected, export = imported(original, home)
    neighbor, neighbor_export = imported(original, home)
    sid, cwd = selected.session_id, str(original.cwd)
    db = home / "cli/db/db.sqlite"
    index = home / "v2/tasks-index.sqlite"
    assert selected.title == original.title
    assert texts(selected) == texts(original)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO input_history VALUES ('history',?,'input')", (sid,))
        c.execute("INSERT INTO session_input VALUES ('input',?,'promoted')", (sid,))
        c.execute("INSERT INTO local_setting VALUES ('session',?,'PRIVATE_MODEL')", (sid,))
        c.execute("INSERT INTO local_setting VALUES ('project','shared','keep')")
        c.execute("INSERT INTO model_usage VALUES ('usage',?,'completed')", (sid,))
        c.execute(
            "INSERT INTO session_entry VALUES ('entry',?,'runtime/model_selection',0,0,?)",
            (sid, '{"modelSelection":{"modelId":"PRIVATE_MODEL"}}'),
        )
    with sqlite3.connect(index) as c:
        row = c.execute(
            "SELECT title,provider,model,meta_json FROM tasks WHERE task_id=?", (sid,)
        ).fetchone()
        assert row[:3] == (original.title, "glm", None)
        assert "model" not in json.loads(row[3])
        c.execute("INSERT INTO task_groups VALUES ('shared')")
        c.execute("INSERT INTO task_group_members VALUES (?,?,'shared')", (cwd, sid))
        c.execute(
            "INSERT INTO task_group_members VALUES (?,?,'shared')", (cwd, neighbor.session_id)
        )
        for task in (sid, neighbor.session_id):
            c.execute(
                "INSERT INTO task_group_view_node_orders VALUES ('task',?)",
                (json.dumps([cwd, task], separators=(",", ":")),),
            )
    selected = core.load(selected.source_path)
    payload, out, manifest = core.prepare(selected, target="pi", output_dir=tmp_path / "out")
    assert b"PRIVATE_MODEL" not in payload
    assert "PRIVATE_MODEL" not in json.dumps(manifest)
    core.apply(selected, payload, out, manifest, move=True)
    assert texts(core.load(out)) == texts(original)
    assert not export.exists() and not export.with_name(export.name + ".manifest.json").exists()
    assert neighbor_export.exists()
    assert [p.name for p in core.source_paths("zcode", home)] == [neighbor.session_id]
    with sqlite3.connect(db) as c:
        for table in (
            "message",
            "part",
            "session_entry",
            "input_history",
            "session_input",
            "model_usage",
        ):
            assert not c.execute(f"SELECT 1 FROM {table} WHERE session_id=?", (sid,)).fetchone()
        assert c.execute("SELECT * FROM local_setting").fetchall() == [
            ("project", "shared", "keep")
        ]
    with sqlite3.connect(index) as c:
        assert c.execute("SELECT task_id FROM tasks").fetchall() == [(neighbor.session_id,)]
        assert c.execute("SELECT task_id FROM task_group_members").fetchall() == [
            (neighbor.session_id,)
        ]
        assert len(c.execute("SELECT * FROM task_group_view_node_orders").fetchall()) == 1
        assert c.execute("SELECT * FROM task_groups").fetchall() == [("shared",)]
    assert original.source_path.exists()


def test_native_sequence_beats_timestamps_and_random_ids(tmp_path):
    home = native_home(tmp_path / "home", desktop=False)
    session, _ = imported(seed(tmp_path / "seed"), home)
    with sqlite3.connect(home / "cli/db/db.sqlite") as c:
        c.execute("UPDATE message SET time_created=0,sequence=10-sequence")
        row = c.execute("SELECT id FROM message ORDER BY sequence LIMIT 1").fetchone()
        part = c.execute("SELECT * FROM part WHERE message_id=?", row).fetchone()
        c.execute("UPDATE part SET sequence=10 WHERE message_id=?", row)
        c.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
            (
                "zzz",
                *part[1:5],
                '{"type":"text","text":"FIRST"}',
                0,
            ),
        )
    assert texts(core.load(session.source_path)) == [
        "FIRST\n修好了 ✓",
        "先运行测试",
        "检查 café.py",
    ]


def test_registration_failure_rolls_back_both_dbs_and_files(tmp_path):
    home = native_home(tmp_path / "home")
    with sqlite3.connect(home / "v2/tasks-index.sqlite") as c:
        c.execute("ALTER TABLE tasks ADD COLUMN new_required TEXT NOT NULL")
    source = seed(tmp_path / "source")
    data, out, manifest = core.prepare(source, target="zcode", target_home=home)
    with pytest.raises(sqlite3.IntegrityError):
        core.apply(source, data, out, manifest, move=True)
    assert source.source_path.exists() and not out.exists()
    for db, table in [
        (home / "cli/db/db.sqlite", "session"),
        (home / "v2/tasks-index.sqlite", "tasks"),
    ]:
        with sqlite3.connect(db) as c:
            assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_cut_file_failure_rolls_back_both_dbs(tmp_path, monkeypatch):
    home = native_home(tmp_path / "home")
    session, export = imported(seed(tmp_path / "seed"), home)
    rename = Path.rename

    def fail(path, target):
        if path == export:
            raise OSError("rename denied")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(OSError, match="denied"):
        stores.remove_source(session)
    assert export.exists() and texts(core.load(session.source_path))
    with sqlite3.connect(home / "v2/tasks-index.sqlite") as c:
        assert c.execute("SELECT task_id FROM tasks").fetchone()[0] == session.session_id


@pytest.mark.parametrize("guard", ["queued", "workflow", "automation", "running"])
def test_cut_refuses_pending_or_linked_sessions(tmp_path, guard):
    home = native_home(tmp_path / "home")
    session, export = imported(seed(tmp_path / "seed"), home)
    if guard == "automation":
        with sqlite3.connect(home / "v2/tasks-index.sqlite") as c:
            c.execute("INSERT INTO automations VALUES ('job',?)", (session.session_id,))
    else:
        with sqlite3.connect(home / "cli/db/db.sqlite") as c:
            if guard == "workflow":
                c.execute("INSERT INTO dwf_actor VALUES (1,?)", (session.session_id,))
            elif guard == "running":
                c.execute("INSERT INTO model_usage VALUES ('u',?,'running')", (session.session_id,))
            else:
                c.execute(
                    "INSERT INTO session_input VALUES ('q',?,'admitted')", (session.session_id,)
                )
    source = core.load(session.source_path)
    data, out, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    with pytest.raises(SessionMigrateError, match="cut refuses"):
        core.apply(source, data, out, manifest, move=True)
    assert not out.exists()
    assert export.exists() and core.load(session.source_path).session_id == session.session_id


def test_changed_native_source_is_retained(tmp_path):
    home = native_home(tmp_path / "home")
    source, export = imported(seed(tmp_path / "seed"), home)
    data, out, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    with sqlite3.connect(home / "cli/db/db.sqlite") as c:
        c.execute("UPDATE session SET title='changed' WHERE id=?", (source.session_id,))
    with pytest.raises(SessionMigrateError, match="source changed"):
        core.apply(source, data, out, manifest, move=True)
    assert export.exists() and out.exists()
    assert core.load(source.source_path).title == "changed"


def test_compaction_uses_latest_summary_and_later_messages(tmp_path):
    source = seed(tmp_path / "seed")
    source = replace(
        source,
        events=source.events
        + (
            Event(EventKind.COMPACTION, Provenance(8), Role.SYSTEM, STAMP, "压缩摘要"),
            Event(EventKind.MESSAGE, Provenance(9), Role.USER, STAMP, "继续"),
            Event(EventKind.MESSAGE, Provenance(10), Role.ASSISTANT, STAMP, "完成"),
        ),
    )
    data, out, manifest = core.prepare(source, target="zcode", output_dir=tmp_path / "out")
    core.apply(source, data, out, manifest)
    parsed = core.load(out)
    assert texts(parsed) == ["压缩摘要", "继续", "完成"]
    assert [e.text for e in parsed.events if e.kind == EventKind.COMPACTION] == ["压缩摘要"]


def test_rewind_excludes_old_branch_and_runtime_parts(tmp_path):
    source = seed(tmp_path / "seed")
    data, out, _ = core.prepare(source, target="zcode", output_dir=tmp_path / "out")
    bundle = json.loads(data)
    messages = bundle["messages"]
    first, discarded, tail = messages
    bundle["info"]["revert"] = json.dumps(
        {
            "targetMessageID": discarded["info"]["id"],
            "branchCutAfterMessageID": discarded["info"]["id"],
            "keptMessageIDs": [first["info"]["id"]],
        }
    )
    hidden = json.loads(json.dumps(tail))
    hidden["info"].update({"id": "hidden", "role": "user", "visibility": "model-only"})
    hidden["parts"][0].update({"messageID": "hidden", "text": "PRIVATE_RUNTIME"})
    hidden["parts"][0]["id"] = "hidden_part"
    messages.append(hidden)
    tail["info"]["summary"] = False
    tail["parts"].append(
        {**tail["parts"][0], "id": "thought_part", "type": "reasoning", "text": "PRIVATE_THOUGHT"}
    )
    out.parent.mkdir()
    out.write_text(json.dumps(bundle), encoding="utf-8")
    parsed = core.load(out)
    assert texts(parsed) == ["检查 café.py", "修好了 ✓"]
    converted, _, manifest = core.prepare(parsed, target="pi", output_dir=tmp_path / "pi")
    assert b"PRIVATE_RUNTIME" not in converted and b"PRIVATE_THOUGHT" not in converted
    assert manifest["dropped_events"]["filtered:thinking"] == 1


def test_compaction_preserves_explicit_segment_after_summary(tmp_path):
    source = seed(tmp_path / "seed")
    source = replace(
        source,
        events=source.events
        + (
            Event(EventKind.COMPACTION, Provenance(8), Role.SYSTEM, STAMP, "摘要"),
            Event(EventKind.MESSAGE, Provenance(9), Role.USER, STAMP, "继续"),
        ),
    )
    data, out, _ = core.prepare(source, target="zcode", output_dir=tmp_path / "out")
    bundle = json.loads(data)
    old_user, old_reply, old_tail, summary, following = bundle["messages"]
    summary["parts"][0]["compactBoundary"] = {
        "preservedSegment": {
            "headMessageId": old_reply["info"]["id"],
            "tailMessageId": old_tail["info"]["id"],
            "anchorMessageId": summary["info"]["id"],
        }
    }
    out.parent.mkdir()
    out.write_text(json.dumps(bundle), encoding="utf-8")
    assert texts(core.load(out)) == ["摘要", "先运行测试", "修好了 ✓", "继续"]
    bundle["info"]["revert"] = {"targetMessageID": old_user["info"]["id"], "keptMessageIDs": []}
    out.write_text(json.dumps(bundle), encoding="utf-8")
    assert texts(core.load(out)) == ["摘要", "先运行测试", "修好了 ✓", "继续"]


def test_tool_results_and_history_models_are_counted_without_content(tmp_path):
    data, out, _ = core.prepare(
        seed(tmp_path / "seed"),
        target="zcode",
        output_dir=tmp_path / "out",
    )
    bundle = json.loads(data)
    message = bundle["messages"][0]
    message["info"]["modelSelection"] = {"providerId": "PRIVATE", "modelId": "PRIVATE"}
    message["parts"].append(
        {
            **message["parts"][0],
            "id": "tool_part",
            "type": "tool",
            "state": {"status": "completed", "output": "PRIVATE_RESULT"},
        }
    )
    out.parent.mkdir()
    out.write_text(json.dumps(bundle), encoding="utf-8")
    data, _, manifest = core.prepare(core.load(out), target="pi", output_dir=tmp_path / "pi")
    assert b"PRIVATE" not in data and "PRIVATE" not in json.dumps(manifest)
    assert manifest["dropped_events"]["filtered:tool_call"] == 1
    assert manifest["dropped_events"]["filtered:tool_result"] == 1
    assert manifest["dropped_events"]["filtered:opaque"] == 1


def test_project_batch_and_exact_virtual_path(tmp_path, capsys):
    home = native_home(tmp_path / "home")
    first, _ = imported(seed(tmp_path / "project"), home)
    imported(seed(tmp_path / "other"), home)
    assert (
        main(
            [
                "list",
                "--from",
                "zcode",
                "--source-root",
                str(home),
                "--project",
                str(tmp_path / "project"),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert [r["session_id"] for r in result["results"]] == [first.session_id]
    assert (
        main(
            [
                "convert",
                str(first.source_path),
                "--to",
                "pi",
                "--output-dir",
                str(tmp_path / "out"),
                "--apply",
                "--move",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert len(core.source_paths("zcode", home)) == 1


def test_portable_move_keeps_native_source(tmp_path):
    home = native_home(tmp_path / "home")
    native, portable = imported(seed(tmp_path / "seed"), home)
    source = core.load(portable)
    data, out, manifest = core.prepare(source, target="pi", output_dir=tmp_path / "out")
    core.apply(source, data, out, manifest, move=True)
    assert not portable.exists()
    assert core.load(native.source_path).session_id == native.session_id


@pytest.mark.parametrize("bad", ["format", "version", "role", "parts", "identity", "revert"])
def test_untrusted_bundle_rejected(tmp_path, bad):
    data, out, _ = core.prepare(
        seed(tmp_path / "seed"), target="zcode", output_dir=tmp_path / "out"
    )
    bundle = json.loads(data)
    if bad == "format":
        bundle["format"] = "unknown"
    elif bad == "version":
        bundle["schema_version"] = 2
    elif bad == "role":
        bundle["messages"][0]["info"]["role"] = "tool"
    elif bad == "parts":
        bundle["messages"][0]["parts"] = None
    elif bad == "identity":
        bundle["messages"][0]["parts"][0]["sessionID"] = "other"
    else:
        bundle["info"]["revert"] = []
    out.parent.mkdir()
    out.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(SessionMigrateError):
        core.load(out, "zcode")
