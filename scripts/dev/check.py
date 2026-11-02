"""Run the shared offline verification used locally and by CI."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    root = Path(__file__).resolve().parents[2]
    commands = [
        [sys.executable, "scripts/dev/check_harness.py"],
        [sys.executable, "-m", "ruff", "check"],
        [sys.executable, "-m", "pytest"],
    ]
    for command in commands:
        print("Running:", " ".join(command[1:]), flush=True)
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
