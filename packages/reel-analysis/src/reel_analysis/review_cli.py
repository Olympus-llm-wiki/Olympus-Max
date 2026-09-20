"""CLI for the compact, resumable review flow."""
import argparse
from . import review_batch


def add_parser(commands):
    command = commands.add_parser("review", help="compact creator review without subtitle extraction")
    sub = command.add_subparsers(dest="review_command", required=True)
    for name in ("plan", "run"):
        parser = sub.add_parser(name, formatter_class=argparse.ArgumentDefaultsHelpFormatter, description="Original videos up to 300 seconds / 100 MiB each; optional context JSON up to 60,000 bytes.")
        parser.add_argument("manifest", help="JSON manifest with source and context hashes")
        parser.add_argument("--output", required=True)
        parser.add_argument("--backend", choices=("zapro", "antigravity"), default="antigravity", help="executor; Zapro requires the selected API key and authorization")
        parser.add_argument("--thinking", choices=("low", "medium", "high"), default="low", help="requested Gemini reasoning profile")
        parser.add_argument("--timeout", type=int, default=180, help="per-request timeout, 30–900 seconds")
        if name == "run":
            parser.add_argument("--workers", type=int, default=8, help="independent simultaneous jobs, 1–8")
            parser.add_argument("--retry-failed", action="store_true")
            parser.add_argument("--format-retries", type=int, choices=(0, 1), default=1, help="one automatic retry of a newly completed unusable JSON response; never a transport-unknown retry")
    status = sub.add_parser("status")
    status.add_argument("--output", required=True)


def dispatch(args):
    if args.review_command == "status":
        return review_batch.status(args.output)
    config = review_batch.configuration(args.backend, args.thinking, args.timeout)
    if args.review_command == "plan":
        return review_batch.plan(args.manifest, args.output, config)
    return review_batch.run(args.manifest, args.output, config, args.workers, args.retry_failed, format_retries=args.format_retries)
