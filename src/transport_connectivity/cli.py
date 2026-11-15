"""Command-line entry point: ``python -m transport_connectivity.cli <command>``."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from transport_connectivity.connector import (
    DATASET_URL,
    DEFAULT_RAW_DIR,
    ConnectorError,
    fetch_full_dataset,
)
from transport_connectivity.processor import (
    DEFAULT_OUTPUT_ROOT,
    ProcessingError,
    process_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m transport_connectivity.cli",
        description="Transport connectivity data pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser(
        "download",
        help="Download and verify the full intercity connectivity CSV.",
        description=(
            "Download the pinned «Если быть точным» archive (or reuse a valid cache), "
            "extract the CSV and write manifest.json. Prints the manifest to stdout."
        ),
    )
    download.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Directory for the archive, CSV and manifest (default: %(default)s).",
    )
    download.add_argument("--url", default=DATASET_URL, help="Source archive URL.")
    download.add_argument(
        "--offline",
        action="store_true",
        help="Use only the cached archive; fail instead of going to the network.",
    )

    process = subparsers.add_parser(
        "process",
        help="Normalize a downloaded snapshot by the data contract.",
        description=(
            "Check the CSV against manifest.json and write cities.csv, route_measurements.csv, "
            "rejected_rows.csv and run-report.json to <output-root>/<snapshot_id>/. "
            "Prints the run report to stdout."
        ),
    )
    process.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Snapshot directory written by download (default: %(default)s).",
    )
    process.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Root for normalized snapshots (default: %(default)s).",
    )
    return parser


def current_code_version() -> str:
    """Commit of the running code, marked dirty when tracked files have local changes."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        changes = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit}-dirty" if changes else commit


def run_download(args: argparse.Namespace) -> int:
    try:
        result = fetch_full_dataset(args.output_dir, url=args.url, offline=args.offline)
    except ConnectorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    origin = "cached archive" if result.used_cache else "downloaded archive"
    print(f"Verified {origin} in {args.output_dir.as_posix()}", file=sys.stderr)
    print(json.dumps(result.manifest.to_dict(), ensure_ascii=False, indent=2))
    return 0


def run_process(args: argparse.Namespace) -> int:
    try:
        report = process_snapshot(
            args.input_dir, args.output_root, code_version=current_code_version()
        )
    except ProcessingError as error:
        print(f"error (status failed): {error}", file=sys.stderr)
        return 1
    balance = report["balance"]
    print(
        f"{report['status']}: {balance['rows_read']} read = "
        f"{balance['route_measurements']} measurements + {balance['rejected_rows']} rejected; "
        f"output in {report['service']['parameters']['output_dir']}",
        file=sys.stderr,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download":
        return run_download(args)
    if args.command == "process":
        return run_process(args)
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
