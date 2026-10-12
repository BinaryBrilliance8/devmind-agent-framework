"""Create isolated, real Git workspaces for engineering runs."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse


class WorkspaceError(RuntimeError):
    pass


async def _git(*args: str, cwd: Path | None = None, timeout: float = 120) -> str:
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k.upper() and "TOKEN" not in k.upper()}
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd) if cwd else None, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            out, err = await proc.communicate()
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise WorkspaceError(f"git {' '.join(args)} exceeded {timeout:g}s") from exc
    if proc.returncode:
        raise WorkspaceError(f"git {' '.join(args)} failed ({proc.returncode}): {(err or out).decode(errors='replace').strip()}")
    return out.decode(errors="replace").strip()


def _is_remote(source: str) -> bool:
    return source.startswith("git@") or urlparse(source).scheme in {"https", "http", "ssh", "git"}


def _enforce_workspace_limits(root: Path, max_files: int, max_bytes: int) -> None:
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build", "coverage"}
    count = total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in ignored and not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                continue
            count += 1
            total += path.stat().st_size
            if count > max_files or total > max_bytes:
                raise WorkspaceError(f"Repository exceeds workspace limits ({max_files:,} files / {max_bytes:,} bytes)")


async def prepare_workspace(source: str | Path, workspace_path: str | Path, *,
                            max_files: int | None = None, max_bytes: int | None = None,
                            command_timeout: float = 120) -> Path:
    """Clone a remote repository or copy a local repository into workspace_path."""
    destination = Path(workspace_path).expanduser().resolve()
    source_text = str(source)
    remote = _is_remote(source_text)
    local_source = None if remote else Path(source_text).expanduser().resolve()
    if local_source and not local_source.is_dir():
        raise WorkspaceError(f"Local repository does not exist: {local_source}")
    if destination.exists() and any(destination.iterdir()):
        raise WorkspaceError(f"Workspace destination must be empty: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        if remote:
            await _git("clone", "--depth", "1", source_text, str(destination), timeout=command_timeout)
        else:
            # A real local git clone preserves repository history and is useful offline.
            await _git("clone", str(local_source), str(destination), timeout=command_timeout)
        if max_files is not None and max_bytes is not None:
            _enforce_workspace_limits(destination, max_files, max_bytes)
        await _git("config", "--local", "user.name", "DevMind", cwd=destination)
        await _git("config", "--local", "user.email", "bot@devmind.local", cwd=destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination
