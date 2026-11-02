"""Exercise corruption detection and the vendored notebook helper without live services."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKS = runpy.run_path(str(ROOT / "scripts/dev/check_harness.py"))
VALIDATE = CHECKS["validate_harness"]
VALIDATE_AGENTS = CHECKS["validate_agent_profiles"]


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    directory = tmp_path / ".agents/skills/test-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        '---\nname: test-skill\ndescription: "Test a local artifact"\n---\n\nInstructions.\n',
        encoding="utf-8",
    )
    (directory / "agents").mkdir()
    (directory / "agents/openai.yaml").write_text(
        'interface:\n  default_prompt: "Use $test-skill."\n', encoding="utf-8"
    )
    files = {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file()
    }
    lock = {
        "version": 1,
        "skills": [{"name": "test-skill", "source": {"kind": "project"}, "files": files}],
    }
    (tmp_path / ".agents/skills-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (tmp_path / "README.md").write_text("Project\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Rules\n", encoding="utf-8")
    return tmp_path


def test_clean_harness_is_accepted(harness: Path) -> None:
    assert VALIDATE(harness) == []


@pytest.mark.parametrize("change", ["tamper", "remove", "extra", "broken-link", "bad-source"])
def test_corrupted_harness_is_rejected(harness: Path, change: str) -> None:
    directory = harness / ".agents/skills/test-skill"
    if change == "tamper":
        with (directory / "SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("Unreviewed instructions.\n")
    elif change == "remove":
        (directory / "SKILL.md").unlink()
    elif change == "extra":
        (directory / "unexpected.py").write_text("print('unexpected')", encoding="utf-8")
    elif change == "broken-link":
        (harness / "README.md").write_text("[Missing](missing.md)\n", encoding="utf-8")
    else:
        path = harness / ".agents/skills-lock.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["skills"][0]["source"] = {"kind": "github", "commit": "main"}
        path.write_text(json.dumps(data), encoding="utf-8")
    assert VALIDATE(harness), f"undetected corruption: {change}"


def test_skill_symlink_is_rejected(harness: Path) -> None:
    link = harness / ".agents/skills/test-skill/leak.md"
    try:
        link.symlink_to(harness / "README.md")
    except OSError:
        pytest.skip("creating symlinks is unavailable in this environment")
    assert any("symlink" in error for error in VALIDATE(harness))


@pytest.mark.parametrize("kind", ["experiment", "tutorial"])
def test_notebook_helper_creates_valid_notebook_and_preserves_existing_file(
    tmp_path: Path, kind: str
) -> None:
    output = tmp_path / f"{kind}.ipynb"
    command = [
        sys.executable,
        str(ROOT / ".agents/skills/jupyter-notebook/scripts/new_notebook.py"),
        "--kind",
        kind,
        "--title",
        "Transport experiment",
        "--out",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["nbformat"] == 4
    assert data["cells"][0]["cell_type"] == "markdown"
    assert "Transport experiment" in "".join(data["cells"][0]["source"])
    original = output.read_bytes()
    repeated = subprocess.run(command, capture_output=True, text=True, check=False)
    assert repeated.returncode != 0
    assert output.read_bytes() == original


@pytest.mark.parametrize("change", [None, "invalid-toml", "missing-field", "ignored-permission"])
def test_agent_profile_validation(tmp_path: Path, change: str | None) -> None:
    directory = tmp_path / ".codex/agents"
    directory.mkdir(parents=True)
    (tmp_path / ".codex/config.toml").write_text(
        "[agents]\nmax_concurrent_threads_per_session = 2\n", encoding="utf-8"
    )
    profile = (
        'name = "reviewer"\ndescription = "Review a concrete diff"\n'
        'developer_instructions = "Return evidence; do not edit source files."\n'
    )
    if change == "invalid-toml":
        profile += "broken = [\n"
    elif change == "missing-field":
        profile = 'name = "reviewer"\ndescription = "Review"\n'
    elif change == "ignored-permission":
        profile += 'sandbox_mode = "read-only"\n'
    (directory / "reviewer.toml").write_text(profile, encoding="utf-8")
    errors = VALIDATE_AGENTS(tmp_path)
    assert bool(errors) == (change is not None)
