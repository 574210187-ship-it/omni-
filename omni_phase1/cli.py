"""CLI for the explicit, one-worker-at-a-time Omni Phase-1 Bitable flow."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import ConfigError, load_config
from .feishu import FeishuAPIError, FeishuClient
from .workflow import OmniWorker, WorkflowError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omni-phase1",
        description="Guarded Drama-only Feishu Bitable queue worker around ClipFlow Visual",
    )
    parser.add_argument(
        "--config",
        default=".workbuddy/job-config.json",
        help="private JSON config; it contains environment references only",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("check-config", help="read-only token, view, and schema check")
    bootstrap = subcommands.add_parser("bootstrap", help="create only missing dedicated worker text fields")
    bootstrap.add_argument("--apply", action="store_true", help="actually create missing worker fields")
    subcommands.add_parser("plan", help="read-only queue classification")
    one = subcommands.add_parser("run-one", help="process one exact eligible record")
    one.add_argument("--record-id", required=True)
    one.add_argument("--apply", action="store_true", help="permit record, attachment, and audit writes")
    drain = subcommands.add_parser("drain", help="process all currently eligible rows serially")
    drain.add_argument("--apply", action="store_true", help="permit record, attachment, and audit writes")
    drain.add_argument("--max-records", type=int, help="optional bounded test limit")
    return parser


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        worker = OmniWorker(config, FeishuClient(config.binding))
        if args.command == "check-config":
            evidence = worker.validate_binding(allow_missing_worker_fields=True)
            _print(
                {
                    "status": "CONFIG_OK",
                    "program_version": __version__,
                    "view_name": config.expected_view_name,
                    "category_value": config.category_value,
                    **evidence,
                }
            )
            return 0
        if args.command == "bootstrap":
            result = worker.bootstrap(apply=args.apply)
            result["program_version"] = __version__
            _print(result)
            return 0
        if args.command == "plan":
            result = worker.plan()
            result["program_version"] = __version__
            _print(result)
            return 0
        if args.command == "run-one":
            if not args.apply:
                raise WorkflowError("APPLY_REQUIRED", "run-one requires --apply; no remote action was taken")
            worker.validate_binding()
            result = worker.process_record(args.record_id)
            result["program_version"] = __version__
            _print(result)
            return 0
        if args.command == "drain":
            if not args.apply:
                raise WorkflowError("APPLY_REQUIRED", "drain requires --apply; no remote action was taken")
            if args.max_records is not None and args.max_records <= 0:
                raise WorkflowError("BAD_MAX_RECORDS", "--max-records must be positive")
            result = worker.drain(max_records=args.max_records)
            result["program_version"] = __version__
            _print(result)
            return 0
        raise AssertionError(f"unhandled command: {args.command}")
    except (ConfigError, WorkflowError, FeishuAPIError) as exc:
        _print(
            {
                "status": "ERROR",
                "program_version": __version__,
                "code": getattr(exc, "code", type(exc).__name__),
                "reason": str(exc),
            }
        )
        return 2
    except KeyboardInterrupt:
        _print({"status": "INTERRUPTED", "program_version": __version__})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
