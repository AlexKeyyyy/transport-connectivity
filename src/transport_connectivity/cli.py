"""Command-line entry point: ``python -m transport_connectivity.cli <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from transport_connectivity.connector import (
    DATASET_URL,
    DEFAULT_RAW_DIR,
    ConnectorError,
    fetch_full_dataset,
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
    return parser


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download":
        return run_download(args)
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
