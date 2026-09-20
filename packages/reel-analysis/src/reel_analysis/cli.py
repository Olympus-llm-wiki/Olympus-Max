import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from .common import ReelError, read_json
from .contracts import Profile
from .pipeline import Worker
from .store import Store


def parser():
    root = argparse.ArgumentParser(description="Durable Reel analysis and compact concurrent review with Gemini")
    default_state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "gemini-reel-analysis"
    root.add_argument("--data-dir", default=os.environ.get("REEL_DATA_DIR", str(default_state)))
    commands = root.add_subparsers(dest="command", required=True)
    from .card_reports import add_parser
    add_parser(commands)
    from .review_cli import add_parser as add_review_parser
    add_review_parser(commands)
    from .zapro_cli import add_parser as add_zapro_parser
    add_zapro_parser(commands)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("file")
    submit = commands.add_parser("submit")
    source = submit.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--asset-id")
    submit.add_argument("--profile")
    submit.add_argument("--course", help="JSON list of explicit source_id/text excerpts")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    for name in ("status", "retry", "revalidate"):
        command = commands.add_parser(name)
        command.add_argument("job_id")
    result = commands.add_parser("result")
    result.add_argument("job_id")
    result.add_argument("--export")
    rerun = commands.add_parser("rerun")
    rerun.add_argument("job_id")
    rerun.add_argument("--track", required=True, choices=["speech", "visual", "all"])
    rerun.add_argument("--profile")
    commands.add_parser("mcp")
    server = commands.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8769)
    backup = commands.add_parser("backup")
    backup.add_argument("target")
    restore = commands.add_parser("restore")
    restore.add_argument("source")
    restore.add_argument("target")
    commands.add_parser("doctor")
    raw_plan = commands.add_parser("raw-plan", help="validate a single-pass raw-extraction batch")
    raw_plan.add_argument("manifest")
    raw_plan.add_argument("--output", required=True)
    raw_plan.add_argument("--model", default="gemini-3.8-flash-high")
    raw_plan.add_argument("--prompt-profile", choices=("detailed-v1", "compact-v2"), default="detailed-v1")
    raw_run = commands.add_parser("raw-run", help="run a raw-extraction batch sequentially")
    raw_run.add_argument("manifest")
    raw_run.add_argument("--output", required=True)
    raw_run.add_argument("--model", default="gemini-3.8-flash-high")
    raw_run.add_argument("--retry-failed", action="store_true")
    raw_run.add_argument("--continue-on-error", action="store_true")
    raw_run.add_argument("--prompt-profile", choices=("detailed-v1", "compact-v2"), default="detailed-v1")
    raw_status = commands.add_parser("raw-status", help="summarize a raw-extraction batch")
    raw_status.add_argument("manifest")
    raw_status.add_argument("--output", required=True)
    raw_status.add_argument("--model", default="gemini-3.8-flash-high")
    raw_status.add_argument("--prompt-profile", choices=("detailed-v1", "compact-v2"), default="detailed-v1")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "zapro":
            from .zapro_cli import dispatch
            result = dispatch(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["state"] != "ready":
                raise SystemExit(2)
            return
        if args.command == "review":
            from .review_cli import dispatch
            result = dispatch(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["counts"]["failed"] or result["counts"]["unknown"]:
                raise SystemExit(2)
            return
        if args.command == "content":
            from .card_reports import dispatch
            result = dispatch(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("state") in ("stale", "missing", "corrupt") or result.get("hold"):
                raise SystemExit(2)
            return
        if args.command in ("raw-plan", "raw-run", "raw-status"):
            from .raw_batch import plan_batch, run_batch, status_batch
            if args.command == "raw-plan":
                result = plan_batch(args.manifest, args.output, args.model, prompt_profile=args.prompt_profile)
            elif args.command == "raw-run":
                result = run_batch(args.manifest, args.output, args.model, args.retry_failed, args.continue_on_error, args.prompt_profile)
            else:
                result = status_batch(args.manifest, args.output, args.model, args.prompt_profile)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "restore":
            store = Store.restore(args.source, args.target)
            print(json.dumps({"restored": str(store.root)}))
            return
        store = Store(args.data_dir)
        if args.command == "ingest":
            info = store.ingest(args.file)
            result = {"asset_id": info["id"], "sha256": info["sha256"], "duration_s": info["duration_s"]}
        elif args.command == "submit":
            profile = Profile(**read_json(args.profile)) if args.profile else Profile()
            asset_id = store.ingest(args.file, profile)["id"] if args.file else args.asset_id
            result = store.submit(asset_id, profile, read_json(args.course) if args.course else None)
        elif args.command == "worker":
            worker = Worker(store)
            if args.once:
                result = worker.run_once()
            else:
                worker.serve()
                return
        elif args.command == "status":
            result = store.get(args.job_id)
        elif args.command == "retry":
            result = store.retry(args.job_id)
        elif args.command == "revalidate":
            result = store.revalidate(args.job_id)
        elif args.command == "rerun":
            result = store.rerun(args.job_id, args.track, Profile(**read_json(args.profile)) if args.profile else None)
        elif args.command == "result":
            folder = store.result(args.job_id)
            if args.export:
                if Path(args.export).exists():
                    raise ReelError("export_target_exists")
                shutil.copytree(folder, args.export)
                folder = Path(args.export)
            result = {"directory": str(folder.resolve()), "json": "analysis.json", "html": "review.html"}
        elif args.command == "backup":
            result = {"backup": str(store.backup(args.target))}
        elif args.command == "doctor":
            from .antigravity import Antigravity
            result = Antigravity().preflight(Profile(), store.root)
        elif args.command == "mcp":
            from .server import mcp_server
            mcp_server(store).run()
            return
        elif args.command == "serve":
            import uvicorn
            from .server import create_app
            uvicorn.run(create_app(store), host=args.host, port=args.port, workers=1, access_log=False)
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ReelError, ValueError, OSError) as exc:
        code = str(exc) if isinstance(exc, ReelError) else type(exc).__name__
        print(json.dumps({"error": code}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
