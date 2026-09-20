"""CLI adapter for project navigation; no model, daemon, or command runner."""
from pathlib import Path

from . import projects as p
from . import workspaces as w


def add_parsers(sub):
    group = sub.add_parser("project", help="Карта подключённых локальных проектов")
    actions = group.add_subparsers(dest="project_action", required=True)
    command = actions.add_parser("register")
    command.add_argument("input", type=Path)
    command.add_argument("--expected", default="new", help="Предыдущая версия либо new")
    actions.add_parser("list")
    for name in ("show", "check"):
        command = actions.add_parser(name)
        command.add_argument("project")
    group = sub.add_parser("workspace", help="Подготовка и продолжение работы в проекте")
    actions = group.add_subparsers(dest="workspace_action", required=True)
    for name in ("prepare", "start"):
        command = actions.add_parser(name)
        if name == "start":
            command.add_argument("work_id")
            command.add_argument("--thread-id")
            command.add_argument("--reference", action="append", default=[])
        command.add_argument("--project", required=True)
        command.add_argument("--goal", required=True)
        command.add_argument("--intent", choices=["read", "edit"], default="read")
        command.add_argument("--resource", action="append", default=[])
        command.add_argument("--path", action="append", default=[], help="resource-id:relative/path")
    command = actions.add_parser("list")
    command.add_argument("--project")
    command = actions.add_parser("show")
    command.add_argument("work_id")
    command.add_argument("--version", help="Прочитать конкретную историческую версию")
    command = actions.add_parser("resume")
    command.add_argument("work_id", nargs="?")
    command.add_argument("--project")
    for name in ("checkpoint", "finish"):
        command = actions.add_parser(name)
        command.add_argument("work_id")
        command.add_argument("input", type=Path)
        command.add_argument("--expected", required=True)


def run(store, args):
    try:
        if args.command == "project":
            if args.project_action == "register":
                return p.register(store, p.load_input(args.input), expected=args.expected)
            if args.project_action == "list":
                return {"projects": [{"version_id": r["version_id"], "id": r["record"]["id"],
                                      "name": r["record"]["name"], "aliases": r["record"]["aliases"]}
                                     for r in p.records(store, "project")]}
            return (p.check if args.project_action == "check" else p.resolve)(store, args.project)
        action = args.workspace_action
        if action in {"prepare", "start"}:
            selection = {key: [] for key in args.resource}
            for item in args.path:
                key, sep, path = item.partition(":")
                if not sep or not key or not path:
                    raise p.PreservationError("workspace_path_requires_resource_id")
                selection.setdefault(key, []).append(path)
            kwargs = dict(goal=args.goal, selection=selection or None, intent=args.intent)
            if action == "start":
                return w.start(store, args.work_id, args.project, thread_id=args.thread_id,
                               references=args.reference, **kwargs)
            return w.prepare(store, args.project, **kwargs)
        if action == "list":
            return w.list_work(store, args.project)
        if action == "show":
            return p.read_record(store, "workspace", args.work_id, version=args.version)
        if action == "resume":
            return w.resume(store, args.work_id, project=args.project)
        return w.checkpoint(store, args.work_id, p.load_input(args.input), expected=args.expected,
                            finish=action == "finish")
    except (KeyError, TypeError, RecursionError):
        raise p.PreservationError("invalid_workspace_record") from None
