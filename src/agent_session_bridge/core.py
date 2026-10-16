"""Selection, conversion and explicit copy/move operations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections import Counter
from dataclasses import replace
from pathlib import Path

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    default_target_home,
    load_session,
    target_import_paths,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, EventKind, Role, Session, TargetFormat

from agent_session_bridge import __version__, dsh, pi_context, stores, workbuddy

FORMATS = ("codex", "pi", "dsh", "opencode", "qwen", "claude", "workbuddy")


def disk_path(path: Path) -> Path:
    text = str(path.absolute())
    if os.name == "nt" and len(text) >= 248 and not text.startswith("\\\\?\\"):
        text = "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
    return Path(text)


def agent_home(agent: str) -> Path:
    if agent == "opencode":
        return (
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
            / "opencode"
        )
    if agent == "workbuddy":
        override = os.environ.get("WORKBUDDY_CONFIG_DIR")
        if override:
            return Path(override).expanduser()
        homes = [Path.home() / ".workbuddy", Path.home() / ".workbuddy-ai"]
        return next((home for home in homes if (home / "projects").is_dir()), homes[0])
    if agent == "dsh":
        return Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))).expanduser()
    return default_target_home(TargetFormat(agent))


def load(path: Path, source_format: str = "auto") -> Session:
    path = disk_path(path)
    if path.parent.name == "opencode.db" and path.parent.is_file():
        if source_format not in {"auto", "opencode"}:
            raise SessionMigrateError("native OpenCode DB requires --from opencode")
        return stores.load_opencode(path)
    if source_format == "auto":
        if path.suffix == ".zstd":
            source_format = "dsh"
        elif path.suffix == ".json":
            # OpenCode is the supported JSON-bundle format; Kilo is not a bridge target.
            source_format = "opencode"
        elif path.suffix == ".jsonl":
            with path.open("r", encoding="utf-8-sig") as stream:
                first = json.loads(stream.readline(1024 * 1024))
            if isinstance(first, dict) and first.get("type") == "session" and "createdAt" in first:
                source_format = "dsh"
            elif (
                isinstance(first, dict) and first.get("type") == "message" and "sessionId" in first
            ):
                source_format = "workbuddy"
            elif isinstance(first, dict) and first.get("type") == "session" and "version" in first:
                source_format = "pi"
    if source_format == "dsh":
        return dsh.parse(path)
    if source_format == "workbuddy":
        return workbuddy.parse(path)
    if source_format == "pi":
        return pi_context.parse(path)
    session = load_session(path, None if source_format == "auto" else AgentFormat(source_format))
    if session.source_format.value not in FORMATS:
        raise SessionMigrateError(f"not a supported bridge format: {session.source_format.value}")
    return session


def source_paths(source_format: str, root: Path | None = None) -> list[Path]:
    if source_format == "auto":
        raise SessionMigrateError("directory/ID/project selection requires --from")
    if source_format == "opencode":
        home = root or agent_home(source_format)
        if (home / "opencode.db").is_file():
            return stores.opencode_paths(home)
        if root is None:
            raise SessionMigrateError("OpenCode DB missing; pass --source-root or an export file")
    if root is None:
        home = agent_home(source_format)
        roots = {
            "codex": [home / "sessions", home / "archived_sessions"],
            "pi": [home / "sessions"],
            "dsh": [home / "sessions"],
            "claude": [home / "projects"],
            "qwen": [home / "projects"],
            "workbuddy": [home / "projects"],
        }[source_format]
    else:
        roots = [root.expanduser()]
        if not roots[0].is_dir():
            raise SessionMigrateError("--source-root must be an existing directory")
    if not any(path.is_dir() for path in roots):
        raise SessionMigrateError(f"no {source_format} session root found; pass --source-root")
    pattern = "*.json" if source_format == "opencode" else "*.jsonl"
    paths = sorted(
        {
            path
            for root in roots
            for path in root.rglob(pattern)
            if path.is_file() and not path.name.endswith(".manifest.json")
        }
    )
    if source_format == "dsh":
        paths += sorted({path for root in roots for path in root.rglob("session*.jsonl.zstd")})
        generations: dict[Path, list[tuple[int, Path]]] = {}
        import re

        for path in paths:
            match = re.fullmatch(r"session(?:\.v([1-9]\d*))?\.jsonl(?:\.zstd)?", path.name)
            if match:
                generations.setdefault(path.parent, []).append((int(match[1] or "0"), path))
        paths = []
        for candidates in generations.values():
            highest = max(version for version, _ in candidates)
            latest = [path for version, path in candidates if version == highest]
            if len(latest) != 1:
                raise SessionMigrateError("DSH session has ambiguous compressed/raw generations")
            paths.extend(latest)
    return sorted(paths)


def project_matches(cwd: Path | None, project: Path | None) -> bool:
    return project is None or (cwd is not None and cwd.resolve().is_relative_to(project.resolve()))


def summary(session: Session) -> dict:
    return {
        "format": session.source_format.value,
        "path": str(session.source_path),
        "session_id": session.session_id,
        "cwd": str(session.cwd) if session.cwd else None,
        "records": session.raw_record_count,
        "events": session.event_counts(),
    }


def prepare(
    session: Session,
    *,
    target: str,
    cwd: Path | None = None,
    target_home: Path | None = None,
    output: Path | None = None,
    output_dir: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    workbuddy_user_id: str | None = None,
) -> tuple[bytes, Path, dict]:
    native_store = None
    home = target_home or agent_home(target) if not (output or output_dir) else None
    if home is not None and target in {"codex", "workbuddy", "opencode"}:
        native_store = stores.descriptor(target, home, user_id=workbuddy_user_id)
    target_id = str(uuid.uuid4())
    working_dir = (cwd or session.cwd or Path.cwd()).expanduser().resolve()
    if not working_dir.is_dir():
        raise SessionMigrateError("target working directory does not exist; pass --cwd")
    events, filtered = [], Counter()
    for event in session.events:
        if (
            (event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT})
            or event.kind == EventKind.COMPACTION
            or (
                event.kind == EventKind.CONTEXT
                and event.role == Role.USER
                and event.payload.get("block_type") == "image"
            )
        ):
            events.append(event)
        else:
            filtered[f"filtered:{event.kind.value}"] += 1
    conversation = replace(session, events=tuple(events))
    if target == "workbuddy":
        payload, dropped = workbuddy.serialize(conversation, session_id=target_id, cwd=working_dir)
        native_path = (
            home / "projects" / workbuddy.project_key(working_dir) / f"{target_id}.jsonl"
            if home
            else Path.cwd() / "migrations" / f"{target_id}.jsonl"
        )
        suffix, target_version = ".jsonl", "5.7.6"
        warnings = [
            {
                "code": "workbuddy_images",
                "message": "WorkBuddy blob images are not copied. Login remains client-owned.",
            }
        ]
    elif target == "dsh":
        payload, dropped = dsh.serialize(
            conversation,
            session_id=target_id,
            cwd=working_dir,
            provider=provider or "imported",
            model=model,
        )
        native_path = (
            (target_home or agent_home(target))
            / "sessions"
            / dsh.project_key(working_dir)
            / target_id
            / "session.v4.jsonl.zstd"
        )
        suffix = ".jsonl.zstd"
        warnings = []
        target_version = "format-v4"
    else:
        # Windows rollout paths contain colons; serializers and path helpers handle these.
        artifact = convert_session(
            conversation,
            ConversionOptions(
                TargetFormat(target),
                session_id=target_id,
                cwd=working_dir,
                model_provider=provider,
                model=model,
            ),
        )
        target_id, payload, dropped = artifact.session_id, artifact.native_bytes, artifact.dropped
        warnings = list(artifact.warnings)
        target_version = artifact.target_cli_version
        suffix = ".json" if target == "opencode" else ".jsonl"
        native_path = (
            (home or Path.cwd() / "migrations") / "asb-imports" / f"{target_id}.json"
            if target == "opencode"
            else target_import_paths(artifact, target_home or agent_home(target))[0]
        )
    destination = disk_path(
        (output or (output_dir / f"{target}-{target_id}{suffix}" if output_dir else native_path))
        .expanduser()
        .absolute()
    )
    if target == "dsh" and not destination.name.endswith(".jsonl.zstd"):
        raise SessionMigrateError("DSH output filename must end in .jsonl.zstd")
    if destination.resolve() == session.source_path.resolve():
        raise SessionMigrateError("source and target must be different files")
    dropped = dict(sorted((Counter(dropped) + filtered).items()))
    manifest = {
        "schema_version": 1,
        "bridge_version": __version__,
        "content": "conversation-only",
        "source": {**summary(session), "sha256": session.source_sha256},
        "target": {
            "format": target,
            "path": str(destination),
            "session_id": target_id,
            "cwd": str(working_dir),
            "cli_version": target_version,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "store": native_store,
        },
        "dropped_events": dropped,
        "warnings": warnings,
        "verification": "sha256-and-parser; native model continuation is not checked",
    }
    return payload, destination, manifest


def _create(path: Path, data: bytes) -> None:
    path = disk_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".asb-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic create-if-absent on NTFS/POSIX; a concurrent file cannot be overwritten.
        os.link(disk_path(temporary_path), path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _source_can_move(session: Session) -> None:
    stores.source_store(session)
    path = session.source_path
    if path.is_symlink():
        raise SessionMigrateError("--move does not delete symlink sources")
    if session.source_format.value == "dsh":
        siblings = [
            item
            for item in path.parent.glob("session*.jsonl*")
            if item != path and not item.name.endswith(".manifest.json")
        ]
        if siblings:
            raise SessionMigrateError(
                "DSH --move refuses multiple generations; use copy or a standalone log"
            )


def apply(
    session: Session, payload: bytes, destination: Path, manifest: dict, *, move: bool = False
) -> dict:
    if manifest["target"].get("store") and not stores.same_path(
        Path(manifest["target"]["path"]), destination
    ):
        raise SessionMigrateError("native destination does not match the prepared manifest")
    if move:
        _source_can_move(session)
    sidecar = disk_path(destination.with_name(destination.name + ".manifest.json"))
    if os.path.lexists(destination) or os.path.lexists(sidecar):
        raise SessionMigrateError("refusing to overwrite an existing target or manifest")
    created: list[tuple[Path, int, int]] = []
    try:
        for path, data in [
            (destination, payload),
            (sidecar, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")),
        ]:
            _create(path, data)
            stat = path.stat()
            created.append((path, stat.st_dev, stat.st_ino))
        actual_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        if actual_hash != manifest["target"]["sha256"]:
            raise SessionMigrateError("target changed after writing; source retained")
        reopened = load(destination, manifest["target"]["format"])
        if reopened.session_id != manifest["target"]["session_id"]:
            raise SessionMigrateError("target session identity failed read-back; source retained")
        if store := manifest["target"].get("store"):
            stores.register(store, manifest, payload, session)
    except BaseException:
        for path, device, inode in reversed(created):
            if path.exists() and (path.stat().st_dev, path.stat().st_ino) == (device, inode):
                path.unlink()
        raise
    if move:
        _source_can_move(session)
        stores.remove_source(session)
    return {
        "status": "moved" if move else "copied",
        "source": str(session.source_path),
        "target": str(destination),
        "manifest": str(sidecar),
        "session_id": manifest["target"]["session_id"],
        "dropped_events": manifest["dropped_events"],
        "native_store": manifest["target"].get("store"),
        "resume": resume_command(
            manifest["target"]["format"],
            destination,
            manifest["target"]["session_id"],
            native=bool(manifest["target"].get("store")),
        ),
    }


def resume_command(target: str, path: Path, session_id: str, *, native: bool = False) -> list[str]:
    return {
        "pi": ["pi", "--session", str(path)],
        "codex": ["codex", "resume", session_id],
        "claude": ["claude", "--resume", session_id],
        "qwen": ["qwen", "--resume", session_id],
        "opencode": (
            ["opencode", "--session", session_id] if native else ["opencode", "import", str(path)]
        ),
        "dsh": ["dsh", "tui", "--resume", session_id],
        "workbuddy": [],
    }[target]
