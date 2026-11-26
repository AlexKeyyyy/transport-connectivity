"""Regression tests for the offline documentation check."""

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
check_links = runpy.run_path(str(ROOT / "scripts/dev/check_docs.py"))["check_links"]


def test_relative_links_and_code_examples(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    (tmp_path / "example file.md").write_text("# Example\n", encoding="utf-8")
    path.write_text(
        "[Relative](example%20file.md#example)\n"
        "![Image](<example file.md>)\n"
        "[External](https://example.org/other.md) [Anchor](#heading)\n"
        "```markdown\n~~~\n[Code sample](missing.md)\n```\n",
        encoding="utf-8",
    )
    assert check_links(tmp_path, [path]) == []


def test_missing_and_outside_targets_report_line_numbers(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "outside.md").touch()
    path = root / "README.md"
    path.write_text(
        "# Example\n[Missing](missing.md)\n[Outside](../outside.md)\n",
        encoding="utf-8",
    )
    assert check_links(root, [path]) == [
        "README.md:2: broken local link missing.md",
        "README.md:3: broken local link ../outside.md",
    ]
