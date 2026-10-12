"""Surgical file editing with syntax validation and Git checkpoints."""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class EditError(RuntimeError):
    pass


@dataclass
class EditResult:
    path: str
    operation: str
    changed: bool
    checkpoint: str | None = None
    message: str = ""


class FileEditor:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self._history: list[tuple[Path, str, bool]] = []

    @staticmethod
    def _write_atomic(target: Path, content: str) -> None:
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent,
                    prefix=".devmind-", suffix=".tmp", delete=False) as stream:
                temp_name = stream.name
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_name, mode)
            os.replace(temp_name, target)
            if target.suffix == ".py":
                # Timestamp-based .pyc validation can accept stale bytecode
                # after a same-size edit within one filesystem timestamp tick.
                # Verification must import the bytes just written, never cache.
                for cache in target.parents[0].rglob("__pycache__"):
                    if cache.is_dir() and not cache.is_symlink():
                        shutil.rmtree(cache, ignore_errors=True)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def _target(self, relative: str | Path) -> Path:
        path = (self.workspace / relative).resolve()
        if not path.is_relative_to(self.workspace):
            raise EditError("Path escapes the workspace")
        return path

    async def _checkpoint(self) -> str | None:
        async def git(*args: str) -> tuple[int, str]:
            env = {k: v for k, v in os.environ.items() if "API_KEY" not in k.upper() and "TOKEN" not in k.upper()}
            p = await asyncio.create_subprocess_exec("git", *args, cwd=self.workspace,
                env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await p.communicate()
            return p.returncode or 0, (out + err).decode(errors="replace").strip()
        code, status = await git("status", "--porcelain")
        if code:
            raise EditError(f"Cannot inspect Git state: {status}")
        # Pytest commonly leaves untracked __pycache__ directories behind. They
        # are not checkpointable with `git commit -am`, and should not prevent
        # editing tracked source files. Only create a checkpoint when tracked
        # files actually have staged or unstaged changes.
        code, unstaged = await git("diff", "--quiet")
        if code not in (0, 1):
            raise EditError(f"Cannot inspect tracked changes: {unstaged}")
        unstaged_changes = code == 1
        code, staged = await git("diff", "--cached", "--quiet")
        if code not in (0, 1):
            raise EditError(f"Cannot inspect staged changes: {staged}")
        staged_changes = code == 1
        if not unstaged_changes and not staged_changes:
            return None
        code, result = await git("commit", "-am", "DevMind checkpoint")
        if code:
            # `-am` intentionally only checkpoints tracked files; untracked edits are not overwritten.
            raise EditError(f"Git checkpoint failed: {result}")
        return result.splitlines()[0] if result else "DevMind checkpoint"

    def _validate(self, target: Path, content: str) -> None:
        if target.suffix == ".py":
            try:
                ast.parse(content, filename=str(target))
            except SyntaxError as exc:
                raise EditError(f"SyntaxError rejected the write: {exc}") from exc

    async def apply(self, operation: str, path: str, *, old: str | None = None,
                    content: str = "", line: int | None = None) -> EditResult:
        if operation not in {"str_replace", "insert"}:
            raise EditError(f"Unsupported edit operation: {operation}")
        target = self._target(path)
        if not target.is_file():
            raise EditError(f"File does not exist: {path}")
        before = target.read_text(encoding="utf-8")
        if operation == "str_replace":
            if not old:
                raise EditError("str_replace requires a non-empty old string")
            count = before.count(old)
            if count != 1:
                raise EditError(f"Expected exactly one match, found {count}")
            after = before.replace(old, content, 1)
        else:
            if line is None or line < 0:
                raise EditError("insert requires a zero-based line")
            lines = before.splitlines(keepends=True)
            if line > len(lines):
                raise EditError(f"Line {line} exceeds file length {len(lines)}")
            inserted = content if content.endswith("\n") else content + "\n"
            after = "".join(lines[:line]) + inserted + "".join(lines[line:])
        if after == before:
            return EditResult(path, operation, False, message="No change")
        # Validate before creating a checkpoint or touching disk.
        self._validate(target, after)
        checkpoint = await self._checkpoint()
        self._history.append((target, before, target.exists()))
        self._write_atomic(target, after)
        return EditResult(path, operation, True, checkpoint, "File updated")

    async def undo_edit(self) -> EditResult:
        if not self._history:
            raise EditError("There is no successful edit to undo")
        target, previous, existed = self._history.pop()
        if existed:
            self._validate(target, previous)
            self._write_atomic(target, previous)
        else:
            target.unlink(missing_ok=True)
        return EditResult(target.relative_to(self.workspace).as_posix(), "undo_edit", True, message="Previous content restored")
