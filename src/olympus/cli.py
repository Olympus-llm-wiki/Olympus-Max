"""Operator commands; normal output contains receipts rather than source text."""
from __future__ import annotations

import argparse
import faulthandler
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .codex_capture import CaptureRegistration
from .delivery import grant_budget, run_one, recall_active
from .hindsight import HindsightClient, HindsightError
from .preservation import Store, PreservationError, guard_no_secrets, timestamp
from .task_capture import register_task, sync_registered_tasks


def _client(args, *, timeout: float = 15.0) -> HindsightClient:
    return HindsightClient(args.url, args.bank, api_key_env=os.environ.get("OLYMPUS_API_KEY_ENV", "OLYMPUS_HINDSIGHT_API_KEY"),
                           timeout=timeout)


def _capture_local(store):
    project = Path(__file__).resolve().parents[2]
    try:
        hook_drain = subprocess.run([sys.executable, str(project / "scripts/codex-capture-hook.py"), "--drain",
                                  "--state-root", str(store.root), "--sessions-root", str(Path.home() / ".codex/sessions")],
                                 cwd=project, capture_output=True, text=True, timeout=8)
        drained = hook_drain.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        drained = False
    result = {"capture": sync_registered_tasks(store)}
    result["hook_signal_drain_ok"] = drained
    return result


def _library(store):
    result = {}
    library_root = store.setting("library_root")
    if library_root:
        from .library import export_versions, scan_notes
        result["notes"] = scan_notes(store, Path(library_root), selected=["Inbox", "Library/Personal"], scope="personal", limit=100)
        result["library"] = export_versions(store, Path(library_root), limit=100)
        from .backup_job import publish_backups
        result["backups"] = publish_backups(store, Path(library_root))
        from .control_state import export_control_state
        result["control_state"] = export_control_state(store, Path(library_root))
    return result


def _collect(store):
    return {**_capture_local(store), **_library(store)}


def _deliver(store, client, limit):
    project = Path(__file__).resolve().parents[2]
    result = {}
    from .runtime_control import RuntimeControl
    try:
        result["runtime"] = RuntimeControl(project).supervise(store)
    except PreservationError as exc:
        result["runtime"] = {"error": str(exc)}
    result["delivery"] = []
    for _ in range(limit):
        step = run_one(store, client)
        result["delivery"].append(step)
        if step["state"] in {"idle", "awaiting_budget", "maintenance", "recovery_blocked"}:
            break
    from .backup_job import maybe_backup
    try:
        result["backup"] = maybe_backup(store, RuntimeControl(project))
    except Exception as exc:
        result["backup"] = {"error": type(exc).__name__}
    store.set_setting("backup_status", json.dumps(result["backup"]))
    return result


def _cycle(store, client, limit):
    return {**_collect(store), **_deliver(store, client, limit)}


def parser():
    p = argparse.ArgumentParser(prog="olympus", description="Локальное сохранение, очередь и проверяемая память.")
    p.add_argument("--state", default=os.environ.get("OLYMPUS_STATE_DIR", str(Path.home() / ".local/state/olympus")))
    p.add_argument("--url", default=os.environ.get("OLYMPUS_API_URL", "http://127.0.0.1:18888"))
    p.add_argument("--bank", default=os.environ.get("OLYMPUS_BANK_ID", "olympus-v1"))
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Создать отдельное локальное состояние")
    init.add_argument("--library", type=Path)
    capture = sub.add_parser("capture-file", help="Атомарно сохранить конкретный источник")
    capture.add_argument("path", type=Path)
    capture.add_argument("--text-file", type=Path, help="Полный извлечённый UTF-8 текст бинарного источника")
    capture.add_argument("--source-key", required=True, help="Устойчивый ID источника, не имя файла")
    capture.add_argument("--scope", required=True)
    capture.add_argument("--title", required=True)
    capture.add_argument("--kind", choices=["document", "note", "owner-decision"], default="document")
    capture.add_argument("--locator")
    capture.add_argument("--metadata", type=Path)
    capture.add_argument("--archive-only", action="store_true", help="Сохранить original архивно без модельного retain; текст может отсутствовать")
    from .materials import ROLES
    capture.add_argument("--role", choices=sorted(ROLES), help="Роль материала, отдельная от достоверности его утверждений")
    url_source = sub.add_parser("capture-url", help="Сохранить публичную HTTP-версию до использования")
    url_source.add_argument("source_url")
    url_source.add_argument("--scope", required=True)
    url_source.add_argument("--title", required=True)
    url_source.add_argument("--source-key")
    for name in ("status", "recover", "sync-tasks", "pause-models"):
        sub.add_parser(name)
    receipt = sub.add_parser("receipt")
    receipt.add_argument("version_id")
    search = sub.add_parser("search", help="Поиск по каталогу и зарегистрированному корпусу")
    search.add_argument("query")
    search.add_argument("--scope", action="append", help="Явно ограничить область; можно повторить")
    search.add_argument("--package", help="Явно ограничить зарегистрированным пакетом")
    search.add_argument("--local-only", action="store_true", help="Без запроса Hindsight")
    search.add_argument("--include-discussions", action="store_true")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--timeout", type=float, default=30)
    query = sub.add_parser("recall")
    query.add_argument("query")
    query.add_argument("--scope", required=True)
    query.add_argument("--timeout", type=float, default=120.0,
                       help="HTTP timeout поиска в секундах: больше 0, не больше 300 (по умолчанию 120)")
    query.add_argument("--package", help="Ограничить выдачу пакетом каталога, например I001")
    query.add_argument("--include-discussions", action="store_true", help="Добавить отдельные локальные выдержки исторических обсуждений")
    budget = sub.add_parser("grant-budget", help="Ограниченный бюджет приёмов источников, не оценка биллинга")
    budget.add_argument("--operations", type=int, required=True)
    budget.add_argument("--minutes", type=int, default=15)
    budget.add_argument("--max-text-chars", type=int, default=50000)
    sub.add_parser("reconcile", help="Применить отзывы в малом банке без страниц знаний")
    sub.add_parser("backup-now", help="Создать checkpoint после проверенного сохранения ключа")
    work = sub.add_parser("work")
    work.add_argument("--limit", type=int, default=5)
    daemon = sub.add_parser("daemon")
    daemon.add_argument("--interval", type=int, default=30)
    daemon.add_argument("--mode", choices=["collect", "library", "delivery", "both"], default="both")
    reg = sub.add_parser("register-task")
    reg.add_argument("thread_id")
    reg.add_argument("transcript_path", type=Path)
    reg.add_argument("--since", default=None)
    reg.add_argument("--scope", required=True)
    reg.add_argument("--codex-version", default="0.153.1")
    forget = sub.add_parser("forget")
    forget.add_argument("source_id")
    forget.add_argument("--reason", required=True)
    supersede = sub.add_parser("supersede")
    supersede.add_argument("version_id")
    supersede.add_argument("replacement_id")
    supersede.add_argument("--reason", required=True)
    retry = sub.add_parser("retry")
    retry.add_argument("version_id")
    migrate_plan = sub.add_parser("migration-plan", help="Зафиксировать отбор I001/I002 и хеши без модельной обработки")
    migrate_plan.add_argument("inventory", type=Path)
    migrate_plan.add_argument("--small-zero-root", type=Path, required=True)
    migrate_plan.add_argument("--package", action="append", choices=["I001", "I002"])
    migrate_apply = sub.add_parser("migration-apply", help="Выполнить зарегистрированный план без повторных версий")
    migrate_apply.add_argument("plan", type=Path)
    migrate_apply.add_argument("--package", choices=["I001", "I002"])
    migrate_status = sub.add_parser("migration-status", help="Проверить сохранность и стадии выбранного пакета")
    migrate_status.add_argument("plan", type=Path)
    migrate_status.add_argument("--package", required=True, choices=["I001", "I002"])
    migrate_review = sub.add_parser("migration-review", help="Сохранить оценку качества и сформировать читаемый отчёт без autoReflect")
    migrate_review.add_argument("plan", type=Path)
    migrate_review.add_argument("--package", required=True, choices=["I001", "I002"])
    migrate_review.add_argument("--assessment-file", type=Path, help="JSON с observations/improvements и ссылками на проверки/items")
    sub.add_parser("migration-catalog", help="Показать карту старых материалов; записи карты не являются текущими решениями")
    from .quality_cli import add_parsers
    add_parsers(sub)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        store = Store(args.state)
        if args.command in {"research", "learning"}:
            from .quality_cli import run
            result = run(store, args)
        elif args.command == "init":
            if args.library:
                root = args.library.expanduser().resolve()
                if not root.is_dir():
                    raise PreservationError("existing_library_root_required")
                store.set_setting("library_root", str(root))
            result = {"state_dir": str(store.root), "initialized": True, "status": store.status()}
        elif args.command == "capture-file":
            path = args.path.expanduser().resolve()
            if path.name in {"auth.json", ".env", "credentials.json"} or path.name.startswith(".env.") or path.suffix in {".key", ".pem"}:
                raise PreservationError("credential_file_not_a_source")
            raw = path.read_bytes()
            text = args.text_file.read_text(encoding="utf-8") if args.text_file else "" if args.archive_only else raw.decode("utf-8")
            meta = json.loads(args.metadata.read_text()) if args.metadata else {}
            if args.role:
                meta["material_role"] = args.role
            result = asdict(store.capture(source_key=args.source_key, scope=args.scope, title=args.title,
                                          original=raw, text=text, locator=args.locator or path.as_uri(),
                                          kind=args.kind, metadata=meta, archive_only=args.archive_only))
        elif args.command == "capture-url":
            from .sources import capture_url
            result = asdict(capture_url(store, args.source_url, scope=args.scope, title=args.title,
                                       source_key=args.source_key))
        elif args.command == "status":
            result = store.status()
            result["budget"] = {"remaining": int(store.setting("budget_remaining", "0")),
                                "expires_at": float(store.setting("budget_expires", "0"))}
            result["recovery"] = store.setting("recovery_state", "ready")
            result["maintenance"] = store.setting("maintenance", "off")
            result["backup"] = json.loads(store.setting("backup_status", '{"state":"waiting_for_recovery_key"}'))
            from .control_state import control_payload
            from .preservation import digest
            with store.connect(write=True) as db:
                remote_control = db.execute("SELECT value FROM settings WHERE key='control_remote_sha'").fetchone()
                result["revocations_remote_verified"] = bool(remote_control and remote_control[0] == digest(control_payload(db)))
                row = db.execute("SELECT value FROM settings WHERE key='backup_last_receipt'").fetchone()
                latest = json.loads(row[0]) if row else None
                result["latest_backup"] = None
                if latest:
                    filename = Path(latest["path"]).name
                    row = db.execute("SELECT value FROM settings WHERE key=?", ("backup_remote:" + filename,)).fetchone()
                    proof = json.loads(row[0]) if row else {}
                    verified = (proof.get("remote_state") == "verified" and proof.get("checkpoint_id") == latest["checkpoint_id"]
                        and proof.get("file", {}).get("sha256") == latest["sha256"] and proof.get("file", {}).get("bytes") == latest["size"])
                    result["latest_backup"] = {"path": latest["path"], "checkpoint_id": latest["checkpoint_id"],
                        "sha256": latest["sha256"], "size": latest["size"], "remote_state": "verified" if verified else "unconfirmed",
                        "remote_checked_at": proof.get("checked_at") if verified else None,
                        "restore_verified": latest.get("restore_verified", False)}
            result["services"] = {mode: {"heartbeat": store.setting("heartbeat:" + mode),
                                           "issues": json.loads(store.setting("issues:" + mode, "[]"))}
                                  for mode in ("collect", "library", "delivery")}
            with store.connect() as db:
                result["registered_tasks"] = [dict(r) for r in db.execute("SELECT thread_id,scope,last_gap FROM registrations")]
                result["locally_captured_events"] = db.execute("SELECT count(*) FROM captured_events").fetchone()[0]
        elif args.command == "receipt":
            result = asdict(store.receipt(args.version_id))
        elif args.command == "recover":
            result = store.recover()
        elif args.command == "grant-budget":
            grant_budget(store, args.operations, args.minutes * 60, args.max_text_chars)
            result = {"admissions": args.operations, "expires_in_minutes": args.minutes, "billing_estimate": False}
        elif args.command == "pause-models":
            with store.exclusive():
                store.set_setting("budget_expires", "0")
            result = {"new_submissions_paused": True, "already_submitted_jobs_cancelled": False}
        elif args.command == "search":
            from .search import search_corpus
            result = search_corpus(store, args.query,
                                   client=None if args.local_only else _client(args, timeout=args.timeout),
                                   scopes=args.scope, package_id=args.package,
                                   include_discussions=args.include_discussions, limit=args.limit)
        elif args.command == "recall":
            guard_no_secrets(args.query.encode())
            result = recall_active(store, _client(args, timeout=args.timeout), args.query, args.scope,
                                   package_id=args.package, include_discussions=args.include_discussions)
        elif args.command == "register-task":
            result = register_task(store, CaptureRegistration(args.thread_id, args.transcript_path,
                                    args.since or timestamp(), args.codex_version), args.scope)
        elif args.command == "sync-tasks":
            result = sync_registered_tasks(store)
        elif args.command == "work":
            if not 1 <= args.limit <= 20:
                raise PreservationError("invalid_cycle_limit")
            result = _cycle(store, _client(args), args.limit)
        elif args.command == "daemon":
            if not 5 <= args.interval <= 3600:
                raise PreservationError("invalid_daemon_interval")
            running = True
            faulthandler.register(signal.SIGUSR1, all_threads=True)
            def stop(signum, frame):
                nonlocal running
                running = False
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            store.recover()
            last_error = None
            while running:
                try:
                    if args.mode == "collect":
                        cycle = _capture_local(store)
                    elif args.mode == "library":
                        cycle = _library(store)
                    elif args.mode == "delivery":
                        cycle = _deliver(store, _client(args), 5)
                    else:
                        cycle = _cycle(store, _client(args), 5)
                    # Keep source content out of service logs and bound output.
                    errors = [x for x in cycle.get("delivery", []) if x.get("error")]
                    if cycle.get("runtime", {}).get("error"):
                        errors.append(cycle["runtime"])
                    errors.extend(cycle.get("notes", {}).get("issues", []))
                    errors.extend(cycle.get("library", {}).get("errors", []))
                    if cycle.get("backup", {}).get("error"):
                        errors.append(cycle["backup"])
                    for task in cycle.get("capture", {}).get("tasks", []):
                        if task.get("coverage_gaps"):
                            errors.append({"thread_id": task["thread_id"], "coverage_gaps": task["coverage_gaps"]})
                    if errors and errors != last_error:
                        print(json.dumps({"at": timestamp(), "errors": errors}), flush=True)
                    last_error = errors
                    store.set_setting("issues:" + args.mode, json.dumps(errors[:20]))
                    store.set_setting("heartbeat:" + args.mode, timestamp())
                except Exception as exc:
                    code = {"error": type(exc).__name__}
                    if code != last_error:
                        print(json.dumps({"at": timestamp(), **code}), flush=True)
                    last_error = code
                deadline = time.monotonic() + args.interval
                while running and time.monotonic() < deadline:
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
            return 0
        elif args.command == "forget":
            result = {"change_id": store.forget(args.source_id, args.reason), "current_read_barrier": True}
        elif args.command == "supersede":
            result = {"change_id": store.supersede(args.version_id, args.replacement_id, args.reason), "current_read_barrier": True}
        elif args.command == "retry":
            receipt = store.receipt(args.version_id)
            client = _client(args)
            with store.exclusive():
                if store.setting("maintenance", "off") != "off" or store.setting("recovery_state", "ready") != "ready":
                    raise PreservationError("maintenance_or_recovery_blocks_retry")
                with store.connect() as db:
                    version = db.execute("SELECT active FROM versions WHERE id=?", (args.version_id,)).fetchone()
                    if version is None or not version[0]:
                        raise PreservationError("version_not_active")
                op = client.operation(receipt.operation_id)
                if op["status"] in {"failed", "cancelled"}:
                    if float(store.setting("budget_expires", "0")) <= time.time():
                        raise PreservationError("retry_requires_current_budget")
                    with store.connect(write=True) as db:
                        remaining = int(db.execute("SELECT value FROM settings WHERE key='budget_remaining'").fetchone()[0])
                        if remaining <= 0:
                            raise PreservationError("retry_requires_current_budget")
                        db.execute("UPDATE settings SET value=? WHERE key='budget_remaining'", (str(remaining - 1),))
                    client.retry_operation(receipt.operation_id)
                store.retry(args.version_id)
            result = asdict(store.receipt(args.version_id))
        elif args.command == "reconcile":
            from .runtime_control import RuntimeControl
            from .maintenance import reconcile_no_pages
            runtime = RuntimeControl(Path(__file__).resolve().parents[2])
            with store.exclusive():
                store.set_setting("maintenance", "reconcile")
                store.set_setting("budget_expires", "0")
            runtime.set_mode("safe")
            try:
                result = reconcile_no_pages(store, _client(args), assert_quiet=runtime.assert_quiet)
            finally:
                store.set_setting("maintenance", "off")
        elif args.command == "backup-now":
            from .runtime_control import RuntimeControl
            from .backup_job import maybe_backup
            result = maybe_backup(store, RuntimeControl(Path(__file__).resolve().parents[2]), force=True)
        elif args.command == "migration-plan":
            from .migration import build_plan, save_plan
            plan = build_plan(args.inventory, args.small_zero_root, Store(store.root / "imports" / "preview"),
                              packages=tuple(args.package or ["I001", "I002"]))
            path = save_plan(store, plan)
            result = {"plan_id": plan["plan_id"], "path": str(path), "files": len(plan["items"]),
                      "catalog_cards": plan["catalog_count"], "issues": plan["issues"]}
        elif args.command == "migration-apply":
            from .migration import apply_plan, write_json
            plan = json.loads(args.plan.read_text())
            run = apply_plan(store, plan, package_id=args.package)
            path = store.root / "imports" / plan["plan_id"] / ("apply-" + (args.package or "all") + ".json")
            write_json(path, run)
            result = {"path": str(path), "processed": len(run["results"]),
                      "local_verified": sum(r.get("local_verified", False) for r in run["results"]),
                      "errors": [r for r in run["results"] if r.get("error")]}
        elif args.command == "migration-status":
            from .migration import package_quality, write_json
            plan = json.loads(args.plan.read_text())
            report = package_quality(store, plan, args.package)
            path = store.root / "imports" / plan["plan_id"] / ("quality-" + args.package + ".json")
            write_json(path, report)
            result = {"path": str(path), "counts": report["counts"], "checks": report["checks"]}
        elif args.command == "migration-catalog":
            state = json.loads(store.setting("legacy_catalog", "{}"))
            if not state:
                raise PreservationError("legacy_catalog_not_registered")
            catalog = json.loads(store.read_version(state["version_id"])["original"])
            result = {"interpretation": "Historical inventory and owner-review fields, not current decisions.",
                      "catalog_version_id": state["version_id"],
                      "cards": [{k: row.get(k) for k in ("id", "title", "category", "description", "locations", "owner_usefulness")} for row in catalog["rows"]]}
        elif args.command == "migration-review":
            from .migration import record_assessment, render_package_report
            plan = json.loads(args.plan.read_text())
            if args.assessment_file:
                assessment = json.loads(args.assessment_file.read_text())
                record_assessment(store, plan, args.package, observations=assessment["observations"],
                                  improvements=assessment.get("improvements", []))
            result = render_package_report(store, plan, args.package)
        else:
            raise PreservationError("unsupported_command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PreservationError, HindsightError) as exc:
        code = str(exc) if isinstance(exc, PreservationError) else exc.code
        print(json.dumps({"error": code}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"error": "input_or_configuration_error", "kind": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
