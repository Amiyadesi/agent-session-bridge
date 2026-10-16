"""Pi's extra runtime roles must not prevent conversation-only imports."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import pi
from session_migrate.model import EventKind, Role


def target_model(
    home: Path, cwd: Path, *, provider: str | None, model: str | None
) -> tuple[str, str]:
    # Pi restores the model from assistant messages, even without model_change entries.
    defaults = {}
    if not (provider and model):
        for path in (home / "settings.json", cwd / ".pi" / "settings.json"):
            if not path.is_file():
                continue
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, dict):
                raise SessionMigrateError(f"Pi settings must be an object: {path}")
            for key in ("defaultProvider", "defaultModel"):
                if key in settings:
                    value = settings[key]
                    if not isinstance(value, str) or not value.strip():
                        raise SessionMigrateError(f"invalid Pi {key}: {path}")
                    defaults[key] = value
    provider = provider or defaults.get("defaultProvider")
    model = model or defaults.get("defaultModel")
    if not provider or not model:
        raise SessionMigrateError(
            "Pi requires destination defaults in settings.json or --provider and --model; "
            "the source model is never reused"
        )
    return provider, model


def parse(path: Path):
    original = path.read_bytes()
    rows = [json.loads(line) for line in original.splitlines() if line.strip()]
    summaries = {}
    changed = False
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            # Let the existing parser report malformed input at its boundary.
            return pi.parse_session(path)
        message = row.get("message")
        if row.get("type") == "message" and isinstance(message, dict):
            role = message.get("role")
            if role in {"system", "bashExecution", "custom", "branchSummary", "compactionSummary"}:
                if role in {"branchSummary", "compactionSummary"}:
                    summaries[index] = message.get("summary")
                row["type"] = "custom"
                changed = True
        elif row.get("type") == "branch_summary":
            summaries[index] = row.get("summary")
    if changed:
        with tempfile.TemporaryDirectory(prefix="asb-pi-") as root:
            temporary = Path(root) / "session.jsonl"
            temporary.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            session = pi.parse_session(temporary)
        session = replace(
            session, source_path=path.resolve(), source_sha256=hashlib.sha256(original).hexdigest()
        )
    else:
        session = pi.parse_session(path)
    events = []
    for e in session.events:
        summary = summaries.get(e.provenance.record_index)
        if (
            summary
            and isinstance(summary, str)
            and e.payload.get("reason") != "inactive_pi_branch_entry"
        ):
            e = replace(e, kind=EventKind.COMPACTION, role=Role.SYSTEM, text=summary)
        events.append(e)
    return replace(session, events=tuple(events))
