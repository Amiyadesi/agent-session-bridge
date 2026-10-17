"""JSON CLI shared by humans and the migration skill."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from compression import zstd
from pathlib import Path

from session_migrate.errors import SessionMigrateError

from agent_session_bridge import __version__, core


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Copy/move conversation context only, without tool traces; JSON output"
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("formats", help="supported formats")
    inspect = commands.add_parser("inspect", help="read one session's metadata, never its messages")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--source-state-db", type=Path, help="Antigravity desktop state.vscdb")
    inspect.add_argument(
        "--from", dest="source_format", choices=("auto", *core.FORMATS), default="auto"
    )
    for name in ("list", "convert"):
        cmd = commands.add_parser(name, help="list sessions" if name == "list" else "copy or move")
        cmd.add_argument("input", type=Path, nargs="?", help="single file or session directory")
        cmd.add_argument(
            "--from", dest="source_format", choices=("auto", *core.FORMATS), default="auto"
        )
        cmd.add_argument("--source-root", type=Path, help="override source session store")
        cmd.add_argument("--source-state-db", type=Path, help="Antigravity desktop state.vscdb")
        cmd.add_argument(
            "--project", type=Path, help="match cwd equal to or beneath this directory"
        )
        cmd.add_argument("--session", help="exact source session ID")
        if name == "convert":
            cmd.add_argument("--to", required=True, choices=core.FORMATS)
            destination = cmd.add_mutually_exclusive_group()
            destination.add_argument("--output", type=Path, help="single target file")
            destination.add_argument(
                "--output-dir", type=Path, help="directory for portable exports"
            )
            destination.add_argument("--target-home", type=Path, help="override native agent home")
            cmd.add_argument("--cwd", type=Path, help="override destination working directory")
            cmd.add_argument("--provider", help="target provider ID")
            cmd.add_argument("--model", help="target model ID")
            cmd.add_argument("--workbuddy-user-id", help="target WorkBuddy account ID")
            cmd.add_argument("--target-state-db", type=Path, help="Antigravity desktop state.vscdb")
            cmd.add_argument(
                "--move", action="store_true", help="delete source after verified write"
            )
            cmd.add_argument("--apply", action="store_true", help="write files (default: preview)")
    return root


def _paths(args: argparse.Namespace) -> list[Path]:
    if args.input and args.source_root:
        raise SessionMigrateError("use input or --source-root, not both")
    if args.input and args.input.is_file():
        if args.input.is_symlink() and getattr(args, "move", False):
            raise SessionMigrateError("--move does not delete symlink sources")
        return [args.input]
    if (args.input and args.input.parent.is_file()
            and args.input.parent.name in {"db.sqlite", "opencode.db"}):
        return [args.input]
    if args.input and not args.input.is_dir():
        raise SessionMigrateError("input does not exist")
    if (
        args.input is None
        and args.source_root is None
        and args.project is None
        and args.session is None
    ):
        raise SessionMigrateError("select an input, --project, or --session")
    return core.source_paths(args.source_format, args.input or args.source_root)


def run(args: argparse.Namespace) -> tuple[dict, int]:
    if args.command == "formats":
        return {
            "formats": list(core.FORMATS),
            "python": "3.14+",
            "engine": "session-migrate 0.11.0",
            "dsh": "v4, JSONL/zstd",
            "default": "preview; keep sources; conversation only (no tool traces)",
        }, 0
    if args.command == "inspect":
        return core.summary(core.load(
            args.input, args.source_format, state_database=args.source_state_db
        )), 0
    paths = _paths(args)
    selected = {}
    if args.session:
        for path in paths:
            session = core.load(path, args.source_format, state_database=args.source_state_db)
            if session.session_id == args.session and core.project_matches(
                session.cwd, args.project
            ):
                selected[path] = session
        if len(selected) > 1:
            raise SessionMigrateError("session ID is ambiguous; select one source file")
        paths = list(selected)
    if args.command == "convert" and args.output and len(paths) != 1:
        raise SessionMigrateError("--output needs one input file; use --output-dir for a batch")
    results, errors = [], []
    for path in paths:
        try:
            if getattr(args, "move", False) and path.is_symlink():
                raise SessionMigrateError("--move does not delete symlink sources")
            session = selected.get(path) or core.load(
                path, args.source_format, state_database=args.source_state_db
            )
            if args.session and session.session_id != args.session:
                continue
            if not core.project_matches(session.cwd, args.project):
                continue
            if args.command == "list":
                results.append(core.summary(session))
                continue
            payload, destination, manifest = core.prepare(
                session,
                target=args.to,
                cwd=args.cwd,
                target_home=args.target_home,
                output=args.output,
                output_dir=args.output_dir,
                provider=args.provider,
                model=args.model,
                workbuddy_user_id=args.workbuddy_user_id,
                target_state_db=args.target_state_db,
            )
            if args.move:
                core._source_can_move(session)
            if args.apply:
                results.append(core.apply(session, payload, destination, manifest, move=args.move))
            else:
                results.append(
                    {
                        "status": "preview",
                        "mode": "move" if args.move else "copy",
                        **manifest,
                        "resume": core.resume_command(
                            args.to,
                            destination,
                            manifest["target"]["session_id"],
                            native=bool(manifest["target"].get("store")),
                        ),
                    }
                )
        except (
            SessionMigrateError,
            sqlite3.Error,
            OSError,
            ValueError,
            UnicodeError,
            EOFError,
            zstd.ZstdError,
        ) as exc:
            errors.append({"source": str(path), "error": str(exc)})
    if not results and not errors:
        errors.append({"error": "no matching sessions"})
    return {
        "command": args.command,
        "apply": getattr(args, "apply", False),
        "count": len(results),
        "results": results,
        "errors": errors,
    }, 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    try:
        result, status = run(parser().parse_args(argv))
    except (
        SessionMigrateError,
        sqlite3.Error,
        OSError,
        ValueError,
        UnicodeError,
        EOFError,
        zstd.ZstdError,
    ) as exc:
        result, status = {"error": str(exc)}, 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return status
