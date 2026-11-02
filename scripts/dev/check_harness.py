"""Check repository skills, their pinned bytes, and local documentation links offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
LINK = re.compile(r"\[[^\]\n]*\]\(([^\s)]+)\)")


def skill_field(text: str, field: str) -> str:
    """Read the two scalar discovery fields; this is not a general YAML parser."""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("missing frontmatter")
    header = text.split("\n---\n", 1)[0]
    values = re.findall(rf"^{field}:\s*(.+)$", header, re.MULTILINE)
    if len(values) != 1:
        raise ValueError(f"expected one {field} field")
    value = values[0].strip()
    if value.startswith('"'):
        value = json.loads(value)
    elif value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty or invalid {field}")
    return value


def safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def check_links(root: Path, paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        fenced = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
            if fenced:
                continue
            for target in LINK.findall(line):
                target = target.strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                destination = (path.parent / unquote(parsed.path)).resolve()
                if not destination.is_relative_to(root) or not destination.exists():
                    errors.append(f"{path.relative_to(root)}: broken local link {target}")
    return errors


def validate_harness(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    if (root / ".agents").is_symlink():
        return [".agents must be a real directory"]
    try:
        lock = json.loads((root / ".agents/skills-lock.json").read_text(encoding="utf-8"))
        if lock["version"] != 1 or not isinstance(lock["skills"], list):
            return ["unsupported skills-lock format"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"cannot read skills-lock: {exc}"]

    skills = root / ".agents/skills"
    if not skills.is_dir() or skills.is_symlink():
        return [".agents/skills must be a real directory"]
    expected_names: set[str] = set()
    owned_docs = [root / "README.md", root / "AGENTS.md"]
    owned_docs.extend((root / "docs").rglob("*.md"))
    for entry in lock["skills"]:
        if not isinstance(entry, dict):
            errors.append("skill entry must be an object")
            continue
        name = entry.get("name", "")
        if not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > 64:
            errors.append(f"invalid skill name: {name!r}")
            continue
        if name in expected_names:
            errors.append(f"duplicate skill: {name}")
        expected_names.add(name)
        directory = skills / name
        if not directory.is_dir() or directory.is_symlink():
            errors.append(f"{name}: missing directory or unsupported symlink")
            continue
        files = entry.get("files")
        if not isinstance(files, dict) or not files or "SKILL.md" not in files:
            errors.append(f"{name}: missing file manifest")
            continue
        actual: dict[str, str] = {}
        has_symlinks = False
        for path in directory.rglob("*"):
            if path.is_symlink():
                errors.append(f"{name}: unsupported symlink {path.name}")
                has_symlinks = True
            elif path.is_file():
                actual[path.relative_to(directory).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        if set(actual) != set(files):
            errors.append(f"{name}: file set differs from lock")
        for relative, digest in files.items():
            if not isinstance(relative, str) or not safe_relative(relative):
                errors.append(f"{name}: invalid manifest path {relative!r}")
            elif not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                errors.append(f"{name}: invalid digest for {relative}")
            elif actual.get(relative) != digest:
                errors.append(f"{name}: missing or changed file {relative}")
        if has_symlinks:
            continue
        try:
            text = (directory / "SKILL.md").read_text(encoding="utf-8")
            if skill_field(text, "name") != name:
                errors.append(f"{name}: discovery name differs from directory")
            description = skill_field(text, "description")
            if len(description) > 1024 or "[TODO:" in text:
                errors.append(f"{name}: description too long or unfinished scaffold")
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: invalid SKILL.md: {exc}")
        source = entry.get("source", {})
        if not isinstance(source, dict) or source.get("kind") not in {"project", "github"}:
            errors.append(f"{name}: missing source")
            continue
        if source["kind"] == "github":
            repo, commit, source_path = (source.get(k, "") for k in ("repo", "commit", "path"))
            if (
                not isinstance(repo, str)
                or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
                or not isinstance(commit, str)
                or not re.fullmatch(r"[0-9a-f]{40}", commit)
                or not isinstance(source_path, str)
                or not safe_relative(source_path)
            ):
                errors.append(f"{name}: invalid pinned GitHub source")
            if "LICENSE.txt" not in files:
                errors.append(f"{name}: missing distributed license")
        else:
            owned_docs.extend(directory.rglob("*.md"))
            metadata = directory / "agents/openai.yaml"
            if not metadata.exists() or f"${name}" not in metadata.read_text(encoding="utf-8"):
                errors.append(f"{name}: missing UI metadata or invocation example")

    actual_names = {p.name for p in skills.iterdir()}
    if actual_names != expected_names:
        errors.append("installed skill set differs from lock")
    for path in owned_docs:
        if not path.is_file():
            errors.append(f"missing document {path.relative_to(root)}")
    errors.extend(check_links(root, [p for p in owned_docs if p.is_file()]))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    errors = validate_harness(args.root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Harness OK: skill discovery, pinned files, provenance and local links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
