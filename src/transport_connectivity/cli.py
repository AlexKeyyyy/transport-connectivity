"""Command-line entry point: ``python -m transport_connectivity.cli <command>``."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from transport_connectivity.air_resilience import analyze_air_resilience
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
from transport_connectivity.rasp_connector import (
    DEFAULT_CITIES_PATH,
    DEFAULT_QUOTA_PATH,
    DEFAULT_RAW_ROOT,
    DailyQuota,
    RaspClient,
    RaspError,
    collect,
    snapshot_dir,
)
from transport_connectivity.rasp_processing import process_rasp_snapshot

PACKAGE_DIR = Path(__file__).resolve().parent


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

    rasp = subparsers.add_parser(
        "rasp-download",
        help="Collect or continue collecting direct trains and flights from Yandex Rasp.",
        description=(
            "Request what is not stored yet for date D, within the daily quota, and write "
            "manifest.json to <raw-root>/yandex_rasp_<D>/. Run again on the next day to "
            "continue. The key is read from the YANDEX_RASP_API_KEY environment variable."
        ),
    )
    rasp.add_argument(
        "--date",
        type=date.fromisoformat,
        required=True,
        help="Service date D, YYYY-MM-DD; one weekday for the whole collection.",
    )
    rasp.add_argument(
        "--cities",
        type=Path,
        default=DEFAULT_CITIES_PATH,
        help="Normalized cities.csv to match (default: %(default)s).",
    )
    rasp.add_argument(
        "--raw-root",
        type=Path,
        default=DEFAULT_RAW_ROOT,
        help="Root for raw snapshots (default: %(default)s).",
    )
    rasp.add_argument(
        "--max-requests",
        type=int,
        help="Stop after this many API calls in this run (the daily quota applies anyway).",
    )
    rasp.add_argument(
        "--offline",
        action="store_true",
        help="Replay stored responses only and report what is still missing.",
    )
    rasp_process = subparsers.add_parser(
        "rasp-process",
        help="Verify a complete Yandex snapshot and normalize direct train and flight services.",
    )
    rasp_process.add_argument("--date", type=date.fromisoformat, required=True)
    rasp_process.add_argument("--cities", type=Path, default=DEFAULT_CITIES_PATH)
    rasp_process.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    rasp_process.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)

    air_resilience = subparsers.add_parser(
        "rasp-air-resilience",
        help="Compare Yandex Rasp air reachability before and after removing Moscow.",
        description="Verify normalized CSVs and print the offline scenario result as JSON.",
    )
    air_resilience.add_argument("--date", type=date.fromisoformat, required=True)
    air_resilience.add_argument(
        "--cities-dir",
        type=Path,
        default=DEFAULT_CITIES_PATH.parent,
        help="Directory with cities.csv and its run-report.json (default: %(default)s).",
    )
    air_resilience.add_argument(
        "--rasp-dir",
        type=Path,
        help="Directory with direct_links.csv and its run-report.json (default: by date).",
    )
    return parser


def current_code_version() -> str:
    """Commit of the running code, marked dirty when tracked files have local changes."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PACKAGE_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        changes = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=PACKAGE_DIR,
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
    except (ProcessingError, OSError) as error:
        print(f"error (status failed): {error}", file=sys.stderr)
        return 1
    balance = report["balance"]
    print(
        f"{report['status']}: {balance['rows_read']} read = "
        f"{balance['route_measurements']} measurements + {balance['rejected_rows']} rejected; "
        f"output in {(args.output_root / report['snapshot_id']).as_posix()}",
        file=sys.stderr,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_rasp_download(args: argparse.Namespace) -> int:
    try:
        client = None
        if not args.offline:
            client = RaspClient(
                os.environ.get("YANDEX_RASP_API_KEY", "").strip(),
                DailyQuota(DEFAULT_QUOTA_PATH),
                max_requests=args.max_requests,
            )
        manifest = collect(
            args.date, raw_root=args.raw_root, cities_path=args.cities, client=client
        )
    except (RaspError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    state = "complete" if manifest["complete"] else f"incomplete ({manifest['stopped_by']})"
    print(
        f"{state}: {manifest['requests_this_run']} requests this run, "
        f"{manifest['stored_responses']} responses stored in "
        f"{snapshot_dir(args.raw_root, args.date).as_posix()}",
        file=sys.stderr,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def run_rasp_process(args: argparse.Namespace) -> int:
    try:
        report = process_rasp_snapshot(
            args.date,
            raw_root=args.raw_root,
            cities_path=args.cities,
            output_root=args.output_root,
            code_version=current_code_version(),
        )
    except (ProcessingError, RaspError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_rasp_air_resilience(args: argparse.Namespace) -> int:
    try:
        report = analyze_air_resilience(
            args.date,
            cities_dir=args.cities_dir,
            rasp_dir=args.rasp_dir or snapshot_dir(DEFAULT_OUTPUT_ROOT, args.date),
        )
    except (ProcessingError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # JSON is printed with Cyrillic as is; the Windows console default (cp1251) garbles it.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "download":
        return run_download(args)
    if args.command == "process":
        return run_process(args)
    if args.command == "rasp-download":
        return run_rasp_download(args)
    if args.command == "rasp-process":
        return run_rasp_process(args)
    if args.command == "rasp-air-resilience":
        return run_rasp_air_resilience(args)
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
