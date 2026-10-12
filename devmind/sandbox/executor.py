"""Bounded subprocess execution inside a repository workspace."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable


@dataclass
class CommandResult:
    command: str
    cwd: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CommandRejected(ValueError):
    pass


class Executor:
    def __init__(self, workspace: str | Path, *, output_limit: int = 15_000, timeout: float = 300):
        self.workspace = Path(workspace).resolve()
        self.output_limit = output_limit
        self.timeout = timeout

    def _cwd(self, relative: str | Path | None) -> Path:
        target = (self.workspace / relative).resolve() if relative else self.workspace
        if not target.is_relative_to(self.workspace):
            raise CommandRejected("Working directory escapes the workspace")
        if not target.is_dir():
            raise CommandRejected(f"Working directory does not exist: {target}")
        return target

    async def execute(self, command: str | list[str], *, cwd: str | Path | None = None, timeout: float | None = None,
                      on_output: Callable[[str, str], Awaitable[None]] | None = None) -> CommandResult:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            raise CommandRejected("Empty command")
        # Explicitly reject shell composition and traversal in command arguments.
        if any(re.search(r"(?:^|[/\\=])\.\.(?:[/\\]|$)", token) for token in argv):
            raise CommandRejected("Path traversal is not allowed")
        if any(token in {"|", "||", "&&", ";", ">", "<", "`"} for token in argv):
            raise CommandRejected("Shell operators are not supported; commands execute as argv")
        run_cwd = self._cwd(cwd)
        env = {k: v for k, v in os.environ.items() if "API_KEY" not in k.upper() and "TOKEN" not in k.upper()}
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(*argv, cwd=run_cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        captured: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = False
        async def drain(stream: asyncio.StreamReader, name: str) -> None:
            nonlocal truncated
            while chunk := await stream.read(65536):
                room = max(0, self.output_limit - sum(map(len, captured.values())))
                if room:
                    captured[name].extend(chunk[:room])
                    if on_output:
                        await on_output(name, chunk[:room].decode(errors="replace"))
                if len(chunk) > room:
                    truncated = True
        try:
            async with asyncio.timeout(timeout or self.timeout):
                await asyncio.gather(drain(proc.stdout, "stdout"), drain(proc.stderr, "stderr"), proc.wait())
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"Command exceeded {timeout or self.timeout:g}s: {' '.join(argv)}")
        return CommandResult(" ".join(argv), str(run_cwd), captured["stdout"].decode(errors="replace"),
            captured["stderr"].decode(errors="replace"), proc.returncode or 0,
            (time.perf_counter() - started) * 1000, truncated)
