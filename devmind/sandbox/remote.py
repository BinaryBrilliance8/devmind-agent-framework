"""Disposable E2B microVM executor used by the public hosted service.

The host keeps only the repository view used by the IDE and patch editor. All
user-controlled commands and tests run in a fresh remote VM without host envs.
"""
from __future__ import annotations

import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from devmind.sandbox.executor import CommandRejected, CommandResult


class RemoteExecutor:
    def __init__(self, workspace: str | Path, *, output_limit: int = 15_000, timeout: float = 300):
        self.workspace = Path(workspace).resolve()
        self.output_limit = output_limit
        self.timeout = timeout
        self.sandbox: Any = None

    async def open(self) -> "RemoteExecutor":
        if not os.getenv("E2B_API_KEY"):
            raise RuntimeError("Public execution is unavailable: E2B_API_KEY is not configured")
        try:
            from e2b import AsyncSandbox
        except ImportError as exc:
            raise RuntimeError("Public execution requires the 'e2b' package") from exc
        # No host environment variables (especially model credentials) are copied.
        self.sandbox = await AsyncSandbox.create(timeout=600, secure=True, allow_internet_access=True,
                                                  metadata={"app": "devmind", "purpose": "public-repository-run"})
        try:
            await self._upload_workspace()
            return self
        except Exception:
            await self.close()
            raise

    async def _upload_workspace(self) -> None:
        count = total = 0
        for path in self.workspace.rglob("*"):
            rel = path.relative_to(self.workspace)
            if any(part in {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"} for part in rel.parts):
                continue
            if path.is_symlink():
                continue
            if not path.is_file():
                continue
            count += 1
            total += path.stat().st_size
            if count > 10_000 or total > 50 * 1024 * 1024:
                raise RuntimeError("Repository exceeds public execution limits (10,000 files / 50 MB)")
            remote = "/workspace/" + rel.as_posix()
            await self.sandbox.files.write(remote, path.read_bytes())
        await self.sandbox.commands.run("git init -q && git config user.name DevMind && git config user.email bot@devmind.local && git add -A && git commit -qm 'DevMind remote workspace snapshot'",
                                        cwd="/workspace", timeout=15)

    async def sync_file(self, relative: str | Path) -> None:
        target = (self.workspace / relative).resolve()
        if not target.is_relative_to(self.workspace) or not target.is_file() or target.is_symlink():
            raise CommandRejected("File sync path escapes workspace or is not a regular file")
        await self.sandbox.files.write("/workspace/" + target.relative_to(self.workspace).as_posix(), target.read_bytes())

    async def execute(self, command: str | list[str], *, cwd: str | Path | None = None,
                      timeout: float | None = None,
                      on_output: Callable[[str, str], Awaitable[None]] | None = None) -> CommandResult:
        if self.sandbox is None:
            raise RuntimeError("Remote sandbox is not open")
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            raise CommandRejected("Empty command")
        if any(re.search(r"(?:^|[/\\=])\.\.(?:[/\\]|$)", item) for item in argv):
            raise CommandRejected("Path traversal is not allowed")
        if any(item in {"|", "||", "&&", ";", ">", "<", "`"} for item in argv):
            raise CommandRejected("Shell operators are not supported; commands execute as argv")
        relative_cwd = Path(cwd or ".")
        if relative_cwd.is_absolute() or ".." in relative_cwd.parts:
            raise CommandRejected("Working directory escapes the workspace")
        remote_cwd = "/workspace" if str(relative_cwd) == "." else "/workspace/" + relative_cwd.as_posix()
        started = time.perf_counter()
        captured = {"stdout": [], "stderr": []}
        used = 0
        truncated = False

        async def on_chunk(name: str, chunk: Any) -> None:
            nonlocal used, truncated
            value = str(chunk)
            room = max(0, self.output_limit - used)
            text = value[:room]
            if len(value) > room:
                truncated = True
            if text:
                used += len(text)
                captured[name].append(text)
                if on_output:
                    await on_output(name, text)

        result = await self.sandbox.commands.run(shlex.join(argv), cwd=remote_cwd,
            timeout=timeout or self.timeout, on_stdout=lambda chunk: on_chunk("stdout", chunk),
            on_stderr=lambda chunk: on_chunk("stderr", chunk))
        return CommandResult(shlex.join(argv), remote_cwd, "".join(captured["stdout"]),
            "".join(captured["stderr"]), int(result.exit_code), (time.perf_counter() - started) * 1000, truncated)

    async def close(self) -> None:
        if self.sandbox is not None:
            sandbox, self.sandbox = self.sandbox, None
            await sandbox.kill()
