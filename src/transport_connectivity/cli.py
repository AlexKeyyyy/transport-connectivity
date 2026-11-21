"""Command-line interface for dataset download and processing."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from transport_connectivity.connector import (
    DATASET_URL,
    ConnectorError,
    fetch_full_dataset,
)
from transport_connectivity.db.load import (
    DEFAULT_NORMALIZED_DIR,
    DEFAULT_RAW_DIR,
    load_snapshot,
)
from transport_connectivity.processor import ProcessingError, process_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m transport_connectivity.cli",
        description="Transport Connectivity dataset connector and processing CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: download
    download_parser = subparsers.add_parser(
        "download",
        help="Download and verify the full intercity connectivity dataset.",
    )
    # Subcommand: migrate
    subparsers.add_parser(
        "migrate",
        help="Run pending PostgreSQL database migrations.",
    )
    download_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/raw/intercity-20260629"),
        help="Target directory for the raw ZIP archive, extracted CSV, and manifest.json.",
    )
    download_parser.add_argument(
        "--url",
        type=str,
        default=DATASET_URL,
        help="Source URL of the dataset ZIP archive.",
    )
    download_parser.add_argument(
        "--offline",
        action="store_true",
        help="Verify and extract only from existing local cache without network requests.",
    )

    load_parser = subparsers.add_parser(
        "load",
        help="Load normalized snapshot data into PostgreSQL",
    )
    load_parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Path to raw snapshot directory containing manifest.json",
    )
    load_parser.add_argument(
        "--normalized-dir",
        type=Path,
        default=DEFAULT_NORMALIZED_DIR,
        help="Path to normalized directory containing run-report.json and CSV files",
    )

    # Subcommand: process
    process_parser = subparsers.add_parser(
        "process",
        help="Normalize the raw connectivity CSV into cities and route_measurements.",
    )
    process_parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to the extracted raw intercity connectivity CSV file.",
    )
    process_parser.add_argument(
        "--manifest",
        type=Path,
        required=False,
        default=None,
        help="Optional path to manifest.json to verify input SHA-256 and snapshot_id.",
    )
    process_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/normalized/tochno_intercity_v20260629"),
        help="Target directory for normalized CSVs and run-report.json.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "download":
        try:
            manifest = fetch_full_dataset(
                args.output_dir,
                url=args.url,
                offline=args.offline,
            )
        except ConnectorError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "migrate":
        import psycopg

        from transport_connectivity.db.migrate import get_connection_string, run_migrations

        try:
            # Test connection first
            with psycopg.connect(get_connection_string()):
                pass
            applied = run_migrations()
            if applied:
                print(f"Applied migrations: {', '.join(applied)}")
            else:
                print("No new migrations to apply. Database is up to date.")
        except Exception as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        return 0

    if args.command == "load":
        try:
            result = load_snapshot(
                raw_dir=args.raw_dir,
                normalized_dir=args.normalized_dir,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if result.already_loaded:
            print(f"Snapshot '{result.snapshot_id}' is already loaded.")
            return 0

        print(f"Loaded snapshot '{result.snapshot_id}':")
        for table, count in result.counts.items():
            print(f"  {table}: {count}")
        return 0

    if args.command == "process":
        try:
            report = process_dataset(
                args.input,
                args.output_dir,
                manifest_path=args.manifest,
            )
        except ProcessingError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
