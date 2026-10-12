"""FastAPI service and static IDE for DevMind."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
import time
import asyncio
import fnmatch
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from devmind.agent.core import AgentRun, PipelineError
from devmind.agent.llm import LLMClient, LLMError, PROVIDERS
from devmind.agent.repomap import build_repomap
from devmind.sandbox.benchmarks import available_benchmarks, create_benchmark
from devmind.sandbox.executor import CommandRejected, Executor
from devmind.sandbox.remote import RemoteExecutor
from devmind.sandbox.workspace import WorkspaceError, prepare_workspace


app = FastAPI(title="DevMind", version="0.1.0")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
STATE: dict[str, dict[str, Path]] = {}
PUBLIC_MODE = os.getenv("DEVMIND_PUBLIC_MODE", "false").lower() in {"1", "true", "yes"}
SESSION_COOKIE = "devmind_session"
ROOT = Path(os.getenv("DEVMIND_WORKSPACES", Path(tempfile.gettempdir()) / "devmind-workspaces")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
_PUBLIC_RUNS: dict[tuple[str, str], list[float]] = {}
_PUBLIC_RUN_SEMAPHORE = asyncio.Semaphore(2)


def _limit_public_action(request: Request, action: str, limit: int) -> None:
    peer = request.client.host if request.client else "unknown"
    key = (peer, action)
    now = time.monotonic()
    recent = [stamp for stamp in _PUBLIC_RUNS.get(key, []) if now - stamp < 3600]
    if len(recent) >= limit:
        raise HTTPException(429, f"Public demo limit reached: {limit} {action} requests per hour per visitor")
    recent.append(now)
    _PUBLIC_RUNS[key] = recent


@app.middleware("http")
async def visitor_session(request: Request, call_next):
    value = request.cookies.get(SESSION_COOKIE, "")
    try:
        uuid.UUID(value)
        session_id = value
    except (ValueError, AttributeError):
        session_id = str(uuid.uuid4())
    request.state.session_id = session_id
    response = await call_next(request)
    if request.cookies.get(SESSION_COOKIE) != session_id:
        response.set_cookie(SESSION_COOKIE, session_id, httponly=True, secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https", samesite="lax", max_age=7 * 24 * 3600)
    return response


class WorkspaceRequest(BaseModel):
    source: str | None = None
    benchmark: str | None = None


class RunRequest(BaseModel):
    issue: str = Field(min_length=5, max_length=12_000)
    provider: str = "openai"
    model: str | None = None
    test_command: str | None = Field(default=None, max_length=2_000)


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=2_000)
    cwd: str | None = None


def current_workspace(request: Request) -> Path:
    value = STATE.get(request.state.session_id)
    workspace = value.get("workspace") if value else None
    if not workspace or not workspace.is_dir():
        raise HTTPException(409, "Open or create a workspace first")
    return workspace


def _detect_test_command(root: Path) -> str | None:
    """Select a declared test suite only when the repository has one."""
    package = root / "package.json"
    if package.is_file():
        try:
            manifest = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            manifest = {}
        script = str(manifest.get("scripts", {}).get("test", "")).strip()
        if script and not re.search(r"no test specified|error:\s*no tests?", script, re.I):
            return "npm test"
        if _has_test_file(root, {".test.js", ".spec.js", ".test.mjs", ".spec.mjs"}):
            return "node --test"
        return None
    has_python_tests = _has_test_file(root, {"test_*.py", "*_test.py"})
    if (root / "pyproject.toml").exists() and has_python_tests:
        return "python3 -m pytest -q"
    if (root / "pytest.ini").exists() or (root / "tox.ini").exists():
        if has_python_tests:
            return "python3 -m pytest -q"
    if _has_test_file(root, {".test.js", ".spec.js", ".test.mjs", ".spec.mjs"}):
        return "node --test"
    if has_python_tests:
        return "python3 -m pytest -q"
    return None


def _has_test_file(root: Path, patterns: set[str]) -> bool:
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build", "coverage"}
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in ignored and not (Path(current) / name).is_symlink()]
        for filename in files:
            if any(filename.endswith(pattern) if pattern.startswith(".") else fnmatch.fnmatch(filename, pattern) for pattern in patterns):
                return True
    return False


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": app.version}


@app.get("/api/providers")
async def providers() -> list[dict[str, Any]]:
    names = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}
    return [{"id": name, "model": data[2], "configured": True if name == "ollama" else bool(os.getenv(names[name]))} for name, data in PROVIDERS.items()]


@app.get("/api/config")
async def public_config() -> dict[str, bool]:
    return {"public_mode": PUBLIC_MODE}


@app.get("/api/benchmarks")
async def benchmarks() -> list[str]:
    return available_benchmarks()


@app.post("/api/workspace")
async def workspace(request: WorkspaceRequest, http_request: Request) -> dict:
    if bool(request.source) == bool(request.benchmark):
        raise HTTPException(422, "Provide exactly one of source or benchmark")
    session_id = http_request.state.session_id
    if PUBLIC_MODE:
        _limit_public_action(http_request, "workspace", 6)
    if "workspace" in STATE.get(session_id, {}):
        shutil.rmtree(STATE[session_id]["workspace"], ignore_errors=True)
    session_root = ROOT / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    destination = session_root / "active"
    shutil.rmtree(destination, ignore_errors=True)
    try:
        if PUBLIC_MODE and request.source:
            parsed = urlparse(request.source)
            parts = parsed.path.strip("/").removesuffix(".git").split("/")
            try:
                allowed_port = parsed.port in (None, 443)
            except ValueError:
                allowed_port = False
            if parsed.scheme != "https" or parsed.hostname != "github.com" or not allowed_port or parsed.username or parsed.password or parsed.query or parsed.fragment or len(parts) != 2 or any(not p or p in {".", ".."} for p in parts):
                raise WorkspaceError("Public mode accepts only https://github.com/{owner}/{repository} public repository URLs")
            if not os.getenv("E2B_API_KEY"):
                raise WorkspaceError("Remote execution is not configured: E2B_API_KEY is missing")
        if request.benchmark:
            path = await create_benchmark(request.benchmark, destination)
        elif PUBLIC_MODE:
            path = await prepare_workspace(request.source or "", destination,
                                           max_files=10_000, max_bytes=50 * 1024 * 1024,
                                           command_timeout=120)
        else:
            path = await prepare_workspace(request.source or "", destination)
    except (WorkspaceError, ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    STATE[session_id] = {"workspace": path}
    origin = f"Benchmark: {request.benchmark}" if request.benchmark else f"Git repository: {request.source}"
    return {"path": str(path), "name": path.name, "origin": origin,
            "tree": _tree(path), "test_command": _detect_test_command(path)}


def _tree(root: Path, rel: str = "", depth: int = 0) -> list[dict]:
    if depth > 5:
        return []
    parent = (root / rel).resolve()
    if not parent.is_relative_to(root) or not parent.is_dir():
        return []
    items = []
    for item in sorted(parent.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name in {".git", ".venv", "node_modules", "__pycache__"}:
            continue
        entry = {"name": item.name, "path": item.relative_to(root).as_posix(), "type": "directory" if item.is_dir() else "file"}
        if item.is_dir():
            entry["children"] = _tree(root, entry["path"], depth + 1)
        items.append(entry)
    return items


@app.get("/api/tree")
async def tree(request: Request) -> dict:
    root = current_workspace(request)
    return {"path": str(root), "tree": _tree(root)}


@app.get("/api/file")
async def read_file(path: str, request: Request) -> dict:
    root = current_workspace(request)
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "File not found")
    if target.stat().st_size > 1_000_000:
        raise HTTPException(413, "File exceeds 1 MB")
    try:
        return {"path": target.relative_to(root).as_posix(), "content": target.read_text(encoding="utf-8")}
    except UnicodeDecodeError as exc:
        raise HTTPException(415, "File is not UTF-8 text") from exc


@app.get("/api/ast")
async def ast_map(request: Request, query: str = "") -> list[dict]:
    return build_repomap(current_workspace(request), query)


@app.get("/api/diff")
async def diff(request: Request) -> dict:
    result = await Executor(current_workspace(request)).execute(["git", "diff", "--no-ext-diff", "--"])
    if result.exit_code:
        raise HTTPException(400, result.stderr)
    return {"diff": result.stdout}


@app.post("/api/terminal")
async def terminal(request: CommandRequest, http_request: Request) -> dict:
    root = current_workspace(http_request)
    executor = RemoteExecutor(root) if PUBLIC_MODE else Executor(root)
    if PUBLIC_MODE:
        _limit_public_action(http_request, "terminal", 12)
    acquired = False
    try:
        if PUBLIC_MODE:
            if _PUBLIC_RUN_SEMAPHORE.locked():
                raise HTTPException(429, "Public execution capacity is busy; try again shortly")
            await _PUBLIC_RUN_SEMAPHORE.acquire()
            acquired = True
            await executor.open()
        result = await executor.execute(request.command, cwd=request.cwd)
    except (CommandRejected, TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if PUBLIC_MODE:
            await executor.close()
            if acquired:
                _PUBLIC_RUN_SEMAPHORE.release()
    return result.to_dict()


@app.post("/api/run")
async def run(request: RunRequest, http_request: Request) -> StreamingResponse:
    root = current_workspace(http_request)
    test_command = request.test_command or _detect_test_command(root)
    if not test_command:
        raise HTTPException(422, "No test command or test suite was detected. Add tests or enter a verification command first.")
    try:
        client = LLMClient(request.provider, request.model)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    if PUBLIC_MODE:
        _limit_public_action(http_request, "agent run", 3)
        if _PUBLIC_RUN_SEMAPHORE.locked():
            raise HTTPException(429, "Public execution capacity is busy; try again shortly")
    async def body():
        executor = RemoteExecutor(root) if PUBLIC_MODE else Executor(root)
        acquired = False
        try:
            if PUBLIC_MODE:
                await _PUBLIC_RUN_SEMAPHORE.acquire()
                acquired = True
                await executor.open()
            agent = AgentRun(root, client, test_command=test_command, executor=executor)
            async for event in agent.events(request.issue):
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except (PipelineError, Exception) as exc:
            yield f"data: {json.dumps({'stage':'failed','error':str(exc)})}\n\n"
        finally:
            if PUBLIC_MODE:
                await executor.close()
                if acquired:
                    _PUBLIC_RUN_SEMAPHORE.release()
        yield "data: [DONE]\n\n"
    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main() -> None:
    import uvicorn
    uvicorn.run("devmind.server:app", host=os.getenv("DEVMIND_HOST", "127.0.0.1"), port=int(os.getenv("PORT", os.getenv("DEVMIND_PORT", "8000"))), reload=False)


if __name__ == "__main__":
    main()
