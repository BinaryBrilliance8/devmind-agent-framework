"""Create isolated, real Git workspaces for engineering runs."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse


class WorkspaceError(RuntimeError):
    pass


async def _git(*args: str, cwd: Path | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k.upper() and "TOKEN" not in k.upper()}
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd) if cwd else None, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode:
        raise WorkspaceError(f"git {' '.join(args)} failed ({proc.returncode}): {(err or out).decode(errors='replace').strip()}")
    return out.decode(errors="replace").strip()


def _is_remote(source: str) -> bool:
    return source.startswith("git@") or urlparse(source).scheme in {"https", "http", "ssh", "git"}


async def prepare_workspace(source: str | Path, workspace_path: str | Path) -> Path:
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
            await _git("clone", "--depth", "1", source_text, str(destination))
        else:
            # A real local git clone preserves repository history and is useful offline.
            await _git("clone", str(local_source), str(destination))
        await _git("config", "--local", "user.name", "DevMind", cwd=destination)
        await _git("config", "--local", "user.email", "bot@devmind.local", cwd=destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination
