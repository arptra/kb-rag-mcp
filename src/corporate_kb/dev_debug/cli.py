"""Developer-facing recording and supervised repair commands."""
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from corporate_kb.dev_debug.interactive import probe
from corporate_kb.dev_debug.process import run_logged
from corporate_kb.dev_debug.recording import SessionStore, now, redact_text
from corporate_kb.dev_debug.workflow import CHECK_IDS, DEFAULT_CHECKS, RepairWorkflow


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Dev logs and supervised GigaCode repair loop")
    result.add_argument("--project", type=Path, default=Path.cwd(), help="Git development checkout")
    commands = result.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("start", "Create a recording session and watch logs until Ctrl+C"),
        ("watch", "Watch logs for an existing session until Ctrl+C"),
        ("run", "Record a foreground dev command (after --), including stderr"),
        ("status", "Show session state and recent events"),
        ("note", "Record reproduction steps, expected result or new observations"),
        ("bundle", "Prepare an immutable diagnostic bundle without invoking GigaCode"),
        ("fix", "Plan, approve, edit, verify and repeat with native GigaCode approvals"),
        ("doctor", "Probe local GigaCode flags without calling a model"),
    ):
        command = commands.add_parser(name, help=help_text)
        if name not in {"doctor", "start"}:
            command.add_argument("--session", default="latest", help="Session ID; default latest")
        if name in {"start", "watch", "run"}:
            command.add_argument(
                "--log",
                action="append",
                type=Path,
                default=[],
                help="Additional log file/directory; repeatable",
            )
        if name in {"start", "run"}:
            command.add_argument("--label", default="", help="Testing scenario")
        if name == "run":
            command.add_argument("argv", nargs=argparse.REMAINDER)
        if name == "status":
            command.add_argument("--follow", action="store_true")
            command.add_argument("--json", action="store_true")
        if name == "note":
            command.add_argument("text", help="Observed behavior; do not include secrets")
        if name == "bundle":
            command.add_argument("--note", default="")
        if name in {"fix", "doctor"}:
            command.add_argument(
                "--gigacode",
                default=os.environ.get("KB_GIGACODE_COMMAND", "gigacode"),
                help="One executable name/path, without extra shell arguments",
            )
        if name == "fix":
            command.add_argument("--goal", default="", help="Reproduction and expected result")
            command.add_argument(
                "--check",
                action="append",
                choices=CHECK_IDS,
                help="Required check; repeatable (default lint,types,skills)",
            )
            command.add_argument("--max-rounds", type=int, default=3)
            command.add_argument("--max-turns", type=int, default=30)
            command.add_argument("--check-timeout", type=float, default=600)
    return result


def session_for(store: SessionStore, requested: str) -> Path:
    session = store.latest() if requested == "latest" else store.root / "sessions" / requested
    if session is None:
        raise ValueError("No recording session. Run ./scripts/dev-debug.sh start first")
    state = store.load(session)  # Validate identity/path before another component can write.
    if Path(state["project_root"]) != store.project_root:
        raise ValueError("This session belongs to another development checkout")
    return session


def add_logs(store: SessionStore, session: Path, paths: list[Path]) -> None:
    if paths:
        state = store.load(session)
        values = list(
            dict.fromkeys(
                [
                    *state.get("extra_log_paths", []),
                    *(
                        str(path if path.is_absolute() else store.project_root / path)
                        for path in paths
                    ),
                ]
            )
        )
        store.update(session, extra_log_paths=values[-100:])


def collect(store: SessionStore, session: Path, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            store.collect_once(session)
        except (OSError, ValueError) as exc:
            print("Collector: " + redact_text(str(exc)), file=sys.stderr, flush=True)
        stop.wait(2)


def show_status(store: SessionStore, session: Path, *, as_json: bool = False) -> None:
    state = store.load(session)
    if as_json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return
    print(f"\nСессия: {session.name}\nАртефакты: {session}")
    print(f"Этап: {state['status']} | Попытка: {state.get('round', 0)}")
    print(f"Задача: {state.get('goal') or state.get('label') or '(добавь fix --goal)'}")
    if state.get("message"):
        print(state["message"])
    last = state.get("last_collection", {})
    print(f"Последний сбор: {last.get('at', 'ещё не было')} | Файлов логов: {last.get('files', 0)}")
    events = session / "events.jsonl"
    if events.is_file() and not events.is_symlink():
        with events.open("rb") as handle:
            handle.seek(max(0, events.stat().st_size - 16 * 1024))
            recent = handle.read(16 * 1024).decode("utf-8", errors="replace").splitlines()
        for line in recent[-6:]:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            timestamp = redact_text(str(event.get("at", "")))[:100]
            kind = redact_text(str(event.get("kind", "")))[:100]
            message = redact_text(str(event.get("message", "")))[:800]
            print(f"  {timestamp} [{kind}] {message}")


def execute(args: argparse.Namespace) -> int:
    if args.action == "doctor":
        result = probe(args.gigacode)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("Probe проверяет бинарник и флаги, но не авторизацию и доступность модели.")
        return 0 if result["available"] else 2
    store = SessionStore(args.project)
    if args.action == "start" or (
        args.action == "run" and args.session == "latest" and store.latest() is None
    ):
        session = store.create(args.label)
    else:
        session = session_for(store, args.session)
    if args.action in {"start", "watch", "run"}:
        add_logs(store, session, args.log)
        print(f"Сессия: {session.name}\nАртефакты: {session}", flush=True)
        print(
            "Автоматическая запись: диагностические артефакты. Ctrl+C останавливает запись.",
            flush=True,
        )
        stop = threading.Event()
        if args.action != "run":
            try:
                collect(store, session, stop)
            finally:
                store.event(session, "watch_stopped", "This log watcher stopped")
            return 0
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not argv:
            raise ValueError("Usage: dev-debug.sh run -- <foreground command> [arguments]")
        recorder = threading.Thread(target=collect, args=(store, session, stop), daemon=True)
        recorder.start()
        try:
            result = run_logged(
                store,
                session,
                argv,
                env={**os.environ, "KB_DEV_DEBUG_CAPTURE": "1"},
                label="dev-server",
            )
            store.update(session, last_process=result)
            return int(result["returncode"])
        finally:
            stop.set()
            recorder.join(timeout=15)
    if args.action == "note":
        state = store.load(session)
        notes = [*state.get("notes", []), {"at": now(), "text": args.text[:8000]}][-30:]
        store.update(session, notes=notes)
        store.event(session, "user_note", args.text)
        print("Наблюдение сохранено: " + str(session))
    elif args.action == "bundle":
        print(store.prepare_bundle(session, note=args.note))
    elif args.action == "status":
        while True:
            show_status(store, session, as_json=args.json)
            if not args.follow:
                break
            time.sleep(5)
    elif args.action == "fix":
        workflow = RepairWorkflow(
            store,
            session,
            command=args.gigacode,
            checks=tuple(args.check or DEFAULT_CHECKS),
            max_rounds=args.max_rounds,
            max_turns=args.max_turns,
            check_timeout=args.check_timeout,
        )
        return workflow.run(args.goal)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return execute(parser().parse_args(argv))
    except KeyboardInterrupt:
        print("\nОстановлено. Записанные артефакты сохранены.")
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(redact_text(str(exc)), file=sys.stderr)
        return 2
