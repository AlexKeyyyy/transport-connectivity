"""Check relative Markdown links without contacting external sites."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

LINK = re.compile(r"\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def check_links(root: Path, paths: list[Path]) -> list[str]:
    """Report local link targets that are missing or outside the project."""
    root = root.resolve()
    errors = []
    for path in paths:
        fence = ""
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            delimiter = FENCE.match(line)
            if fence:
                if delimiter and delimiter[1].startswith(fence) and not delimiter[2].strip():
                    fence = ""
                continue
            if delimiter:
                fence = delimiter[1]
                continue

            for angle_target, plain_target in LINK.findall(line):
                target = angle_target or plain_target
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                destination = (path.parent / unquote(parsed.path)).resolve()
                if not destination.is_relative_to(root) or not destination.exists():
                    errors.append(f"{path.relative_to(root)}:{number}: broken local link {target}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    root = parser.parse_args().root.resolve()
    paths = [
        root / "README.md",
        root / "data/README.md",
        root / "benchmarks/README.md",
        root / "scripts/README.md",
        *sorted((root / "docs").rglob("*.md")),
        *sorted((root / "deliverables").rglob("*.md")),
    ]
    errors = check_links(root, paths)
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    print(f"Local links checked in {len(paths)} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
