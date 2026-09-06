"""Command routing for evidence and reviewed learning; all operations are local."""
from pathlib import Path

from . import evidence as ev
from . import learning as learn


def add_parsers(sub):
    research = sub.add_parser("research", help="Пакеты синтеза и проверка оснований без модели")
    actions = research.add_subparsers(dest="research_action", required=True)
    p = actions.add_parser("register", help="Сохранить manifest и выбрать текущую версию пакета")
    p.add_argument("input", type=Path)
    for name in ("check", "show"):
        p = actions.add_parser(name)
        p.add_argument("version_id")
    p = actions.add_parser("list")
    p.add_argument("--scope", required=True)
    learning = sub.add_parser("learning", help="Наблюдения, проверка предложений и активация по решению")
    actions = learning.add_subparsers(dest="learning_action", required=True)
    for name in ("record", "propose"):
        p = actions.add_parser(name)
        p.add_argument("input", type=Path)
    for name in ("check", "show"):
        p = actions.add_parser(name)
        p.add_argument("version_id")
    for name in ("list", "policy"):
        p = actions.add_parser(name)
        p.add_argument("--scope", required=True)
    p = actions.add_parser("activate")
    p.add_argument("version_id")
    p.add_argument("--expected-evaluation", required=True)
    p.add_argument("--approval-version", required=True)
    p = actions.add_parser("reset")
    p.add_argument("--scope", required=True)
    p.add_argument("--expected-policy", required=True)
    p.add_argument("--approval-version", required=True)
    p = actions.add_parser("from-migration")
    p.add_argument("plan", type=Path)
    p.add_argument("--package", required=True, choices=["I001", "I002"])
    p.add_argument("--id", required=True)
    p.add_argument("--scope", required=True)


def run(store, args):
    try:
        return _run(store, args)
    except (KeyError, TypeError, RecursionError):
        raise ev.PreservationError("invalid_quality_record") from None


def _run(store, args):
    if args.command == "research":
        action = args.research_action
        if action == "register":
            return ev.register_package(store, ev.load_input(args.input))
        if action == "check":
            return ev.check_package(store, args.version_id)
        if action == "show":
            with store.exclusive():
                data, _ = ev.read_record(store, args.version_id, "research_package")
                ev.assert_ready(store, data["scope"])
                return {"version_id": args.version_id, "manifest": data,
                        "verification": ev.check_package(store, args.version_id)}
        with store.exclusive():
            ev.assert_ready(store, args.scope)
            result = []
            for vid in ev.registry(store, "research_package", args.scope):
                try:
                    checked = ev.check_package(store, vid, require_current=True)
                    result.append({"package_version": vid, "package_id": checked["package_id"],
                                   "state": checked["state"], "claim_count": len(checked["claims"]),
                                   "issue_codes": sorted({x["code"] for x in checked["issues"]})})
                except ev.PreservationError as exc:
                    result.append({"package_version": vid, "state": "held", "reason": str(exc)})
            return {"scope": args.scope, "packages": result}
    action = args.learning_action
    if action == "record":
        return learn.record_case(store, ev.load_input(args.input))
    if action == "propose":
        return learn.propose_policy(store, ev.load_input(args.input))
    if action == "check":
        return learn.evaluate_policy(store, args.version_id)
    if action == "list":
        return {"scope": args.scope, "cases": learn.list_cases(store, args.scope)}
    if action == "policy":
        return learn.effective_policy(store, args.scope)
    if action == "activate":
        return learn.activate_policy(store, args.version_id, expected_evaluation=args.expected_evaluation,
                                     approval_version=args.approval_version)
    if action == "reset":
        return learn.reset_policy(store, args.scope, expected_policy=args.expected_policy,
                                  approval_version=args.approval_version)
    if action == "from-migration":
        return learn.from_migration(store, ev.load_input(args.plan, limit=8 * 1024**2), args.package,
                                    record_id=args.id, scope=args.scope)
    with store.exclusive():
        version = ev.Sources(store).get(args.version_id)
        kind = version["metadata"].get("artifact_type")
        if kind not in {"learning_case", "learning_proposal", "learning_activation"}:
            raise ev.PreservationError("wrong_learning_record_type")
        data, _ = ev.read_record(store, args.version_id, kind)
        ev.assert_ready(store, data["scope"])
        return {"version_id": args.version_id, "kind": kind, "record": data}
