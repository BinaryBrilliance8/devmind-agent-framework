"""Litmus tests for physical Git ingestion and the syntax-guarded editor."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from devmind.agent.tools import EditError, FileEditor
from devmind.sandbox.benchmarks import available_benchmarks, create_benchmark
from devmind.sandbox.executor import CommandRejected, Executor
from devmind.sandbox.workspace import prepare_workspace


PUBLIC_REPO = os.getenv("DEVMIND_PUBLIC_TEST_REPO", "https://github.com/octocat/Hello-World.git")


def test_git_ingestion_clones_public_repository_and_sets_identity(tmp_path: Path) -> None:
    destination = tmp_path / "cloned"
    path = asyncio.run(prepare_workspace(PUBLIC_REPO, destination))
    assert path == destination.resolve()
    assert any(p.is_file() for p in path.iterdir())
    name = subprocess.run(["git", "config", "--local", "user.name"], cwd=path, text=True, capture_output=True, check=True).stdout.strip()
    email = subprocess.run(["git", "config", "--local", "user.email"], cwd=path, text=True, capture_output=True, check=True).stdout.strip()
    depth = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=path, text=True, capture_output=True, check=True).stdout.strip()
    assert (name, email) == ("DevMind", "bot@devmind.local")
    assert depth == "1", "remote ingestion should use a shallow clone"


def test_ast_gate_rejects_invalid_write_without_touching_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "--local", "user.name", "DevMind"], cwd=root, check=True)
    subprocess.run(["git", "config", "--local", "user.email", "bot@devmind.local"], cwd=root, check=True)
    target = root / "module.py"
    original = "def greet(name):\n    return f'hello {name}'\n"
    target.write_text(original, encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    with pytest.raises(EditError, match="SyntaxError"):
        asyncio.run(FileEditor(root).apply("str_replace", "module.py", old="return f'hello {name}'", content="return f'hello {name}'\n    if True"))
    assert target.read_text(encoding="utf-8") == original
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, check=True).stdout
    assert status == "", "syntax rejection must not checkpoint or modify the worktree"


def test_valid_edit_writes_and_undo_restores_content(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "--local", "user.name", "DevMind"], cwd=root, check=True)
    subprocess.run(["git", "config", "--local", "user.email", "bot@devmind.local"], cwd=root, check=True)
    target = root / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    editor = FileEditor(root)
    result = asyncio.run(editor.apply("str_replace", "module.py", old="VALUE = 1", content="VALUE = 2"))
    assert result.changed and target.read_text(encoding="utf-8") == "VALUE = 2\n"
    undo = asyncio.run(editor.undo_edit())
    assert undo.changed and target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_executor_runs_real_process_scrubs_secrets_and_bounds_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMPLE_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("SAMPLE_TOKEN", "must-not-reach-child")
    executor = Executor(tmp_path, output_limit=200)
    result = asyncio.run(executor.execute(["python3", "-c", "import os; print(os.getenv('SAMPLE_API_KEY'), os.getenv('SAMPLE_TOKEN'))"]))
    assert result.exit_code == 0
    assert result.stdout.strip() == "None None"
    long = asyncio.run(executor.execute(["python3", "-c", "print('x' * 5000)"]))
    assert len(long.stdout) <= 200
    assert long.truncated
    with pytest.raises(CommandRejected):
        asyncio.run(executor.execute(["cat", "../outside"], cwd="."))


@pytest.mark.parametrize("name", available_benchmarks())
def test_benchmark_template_is_a_real_failing_multifile_git_repo(tmp_path: Path, name: str) -> None:
    root = asyncio.run(create_benchmark(name, tmp_path / name))
    assert len(list(root.rglob("*.py"))) >= 3
    assert (root / "tests").is_dir()
    result = subprocess.run(["python3", "-m", "pytest", "-q"], cwd=root, text=True, capture_output=True)
    assert result.returncode != 0, "benchmark's checked-in bug should fail its regression suite before patching"
