"""FastAPI service and static IDE for DevMind."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from devmind.agent.core import AgentRun, PipelineError
from devmind.agent.llm import LLMClient, LLMError, PROVIDERS
from devmind.agent.repomap import build_repomap
from devmind.sandbox.benchmarks import available_benchmarks, create_benchmark
from devmind.sandbox.executor import CommandRejected, Executor
from devmind.sandbox.workspace import WorkspaceError, prepare_workspace


app = FastAPI(title="DevMind", version="0.1.0")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
STATE: dict[str, Path] = {}
ROOT = Path(os.getenv("DEVMIND_WORKSPACES", Path(tempfile.gettempdir()) / "devmind-workspaces")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)


class WorkspaceRequest(BaseModel):
    source: str | None = None
    benchmark: str | None = None


class RunRequest(BaseModel):
    issue: str = Field(min_length=5, max_length=12_000)
    provider: str = "openai"
    model: str | None = None
    test_command: str = "python3 -m pytest -q"


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=2_000)
    cwd: str | None = None


def current_workspace() -> Path:
    value = STATE.get("workspace")
    if not value or not value.is_dir():
        raise HTTPException(409, "Open or create a workspace first")
    return value


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


@app.get("/api/benchmarks")
async def benchmarks() -> list[str]:
    return available_benchmarks()


@app.post("/api/workspace")
async def workspace(request: WorkspaceRequest) -> dict:
    if bool(request.source) == bool(request.benchmark):
        raise HTTPException(422, "Provide exactly one of source or benchmark")
    if "workspace" in STATE:
        shutil.rmtree(STATE["workspace"], ignore_errors=True)
    destination = ROOT / "active"
    shutil.rmtree(destination, ignore_errors=True)
    try:
        if request.benchmark:
            path = await create_benchmark(request.benchmark, destination)
        else:
            path = await prepare_workspace(request.source or "", destination)
    except (WorkspaceError, ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    STATE["workspace"] = path
    return {"path": str(path), "name": path.name, "tree": _tree(path)}


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
async def tree() -> dict:
    root = current_workspace()
    return {"path": str(root), "tree": _tree(root)}


@app.get("/api/file")
async def read_file(path: str) -> dict:
    root = current_workspace()
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
async def ast_map(query: str = "") -> list[dict]:
    return build_repomap(current_workspace(), query)


@app.get("/api/diff")
async def diff() -> dict:
    result = await Executor(current_workspace()).execute(["git", "diff", "--no-ext-diff", "--"])
    if result.exit_code:
        raise HTTPException(400, result.stderr)
    return {"diff": result.stdout}


@app.post("/api/terminal")
async def terminal(request: CommandRequest) -> dict:
    try:
        result = await Executor(current_workspace()).execute(request.command, cwd=request.cwd)
    except (CommandRejected, TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return result.to_dict()


@app.post("/api/run")
async def run(request: RunRequest) -> StreamingResponse:
    root = current_workspace()
    try:
        client = LLMClient(request.provider, request.model)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    agent = AgentRun(root, client, test_command=request.test_command)
    async def body():
        try:
            async for event in agent.events(request.issue):
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except (PipelineError, Exception) as exc:
            yield f"data: {json.dumps({'stage':'failed','error':str(exc)})}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main() -> None:
    import uvicorn
    uvicorn.run("devmind.server:app", host=os.getenv("DEVMIND_HOST", "127.0.0.1"), port=int(os.getenv("DEVMIND_PORT", "8000")), reload=False)


if __name__ == "__main__":
    main()
