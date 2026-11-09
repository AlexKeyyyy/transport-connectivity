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
