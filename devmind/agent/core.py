"""Five-stage, execution-grounded repository engineering pipeline."""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from devmind.agent.llm import LLMClient, LLMError
from devmind.agent.repomap import build_repomap
from devmind.agent.tools import EditError, FileEditor
from devmind.sandbox.executor import Executor


SYSTEM = """You are a repository engineer. Use only the supplied AST map, source excerpts, issue, and test output. If explicit_target_paths is non-empty, you MUST choose one of those exact paths. Do not touch unrelated code. Choose the AST symbol and source excerpt that directly match the issue. Return exactly one JSON object with this schema: {\"reasoning\": string, \"operation\": \"str_replace\"|\"insert\", \"path\": workspace-relative path, \"old\": exact existing text (str_replace only), \"content\": replacement/insert text, \"line\": zero-based insertion line (insert only)}. Use no markdown fences and no extra JSON objects. Make the smallest correct patch. Do not claim tests passed."""


class PipelineError(RuntimeError):
    pass


class AgentRun:
    def __init__(self, workspace: str | Path, client: LLMClient, *, test_command: str = "python3 -m pytest -q"):
        self.workspace = Path(workspace).resolve()
        self.client = client
        self.executor = Executor(self.workspace)
        self.editor = FileEditor(self.workspace)
        self.test_command = test_command

    async def _execute_events(self, command: str):
        """Yield real stdout/stderr chunks as the child process produces them."""
        import asyncio
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        async def capture(name: str, text: str) -> None:
            await queue.put((name, text))
        task = asyncio.create_task(self.executor.execute(command, on_output=capture))
        while not task.done():
            try:
                name, text = await asyncio.wait_for(queue.get(), timeout=0.1)
                yield "output", {"stream": name, "text": text}
            except TimeoutError:
                pass
        while not queue.empty():
            name, text = queue.get_nowait()
            yield "output", {"stream": name, "text": text}
        yield "result", await task

    async def events(self, issue: str) -> AsyncIterator[dict]:
        async def emit(stage: str, **data: object) -> dict:
            return {"stage": stage, **data}
        # 1. AST Localize
        symbols = build_repomap(self.workspace, issue)
        yield await emit("localize", symbols=symbols)
        # 2. Reproduce Bug. A non-zero test suite is evidence for the reported defect.
        initial = None
        async for kind, data in self._execute_events(self.test_command):
            if kind == "output":
                yield await emit("terminal", phase="reproduce", **data)
            else:
                initial = data
        assert initial is not None
        yield await emit("reproduce", command=initial.to_dict())
        if initial.exit_code == 0:
            yield await emit("reproduce", note="Baseline suite passes; continuing to address the request.")
        elif initial.exit_code not in (1, 5):
            raise PipelineError(f"Baseline test command failed to execute (exit {initial.exit_code}): {initial.stderr}")
        # 3. Surgical Patch
        explicit_paths = sorted({symbol["path"] for symbol in symbols if symbol["path"] in issue})
        explicit_symbols = [symbol for symbol in symbols if symbol["path"] in explicit_paths]
        candidate_symbols = explicit_symbols[:8] if explicit_symbols else symbols[:8]
        source_context = []
        context_size = 0
        for symbol in candidate_symbols:
            if context_size >= 30_000 or len(source_context) >= 8:
                break
            source_path = (self.workspace / symbol["path"]).resolve()
            if not source_path.is_relative_to(self.workspace):
                continue
            try:
                lines = source_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            start = max(0, symbol["line"] - 16)
            end = min(len(lines), symbol["end_line"] + 24)
            excerpt = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[start:end], start))
            excerpt = excerpt[:max(0, 30_000 - context_size)]
            source_context.append({"path": symbol["path"], "start_line": start + 1, "source": excerpt})
            context_size += len(excerpt)
        prompt = {"issue": issue, "explicit_target_paths": explicit_paths,
                  "ast_symbols": candidate_symbols, "baseline_tests": initial.to_dict(),
                  "source_context": source_context,
                  "instructions": "Return one JSON edit operation. Only target an existing file in this workspace. If explicit_target_paths is non-empty, you MUST edit a path from that list. The source excerpts include line numbers for reference; use exact source text in old when replacing."}
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(prompt)}]
        result = None
        for attempt in range(2):
            text = ""
            try:
                async for event in self.client.stream(messages):
                    if event.type == "token":
                        text += event.data["text"]
                        yield await emit("model_token", text=event.data["text"])
                    else:
                        yield await emit("telemetry", attempt=attempt + 1, **event.data)
            except LLMError as exc:
                raise PipelineError(str(exc)) from exc
            try:
                patch = _parse_patch(text)
                reasoning = patch.get("reasoning", "")
                yield await emit("reasoning", text=reasoning)
                operation = patch["operation"]
                if operation not in {"str_replace", "insert"}:
                    raise ValueError(f"Unsupported model operation {operation}")
                if explicit_paths and patch["path"] not in explicit_paths:
                    raise EditError(f"Issue explicitly targets {', '.join(explicit_paths)}; edit a file from that list")
                result = await self.editor.apply(operation, patch["path"], old=patch.get("old"), content=patch.get("content", ""), line=patch.get("line"))
                yield await emit("tool", tool="file_editor", result=result.__dict__)
                break
            except (ValueError, KeyError, EditError) as exc:
                if attempt:
                    raise PipelineError(f"Patch rejected after one correction attempt: {exc}") from exc
                messages.extend([
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"Your proposed edit was rejected before it changed the file. Error: {exc}\nReturn a corrected JSON edit operation only."},
                ])
                yield await emit("patch_rejected", error=str(exc), correction_attempt=2)
        assert result is not None
        # 4. Verify
        verification = None
        try:
            async for kind, data in self._execute_events(self.test_command):
                if kind == "output":
                    yield await emit("terminal", phase="verify", **data)
                else:
                    verification = data
        except Exception as exc:
            undo = await self.editor.undo_edit()
            yield await emit("reflect", outcome="verification_execution_failed", rollback=undo.__dict__)
            raise PipelineError(f"Verification command failed and the edit was rolled back: {exc}") from exc
        assert verification is not None
        yield await emit("verify", command=verification.to_dict())
        # 5. Reflect / Rollback
        if verification.exit_code:
            undo = await self.editor.undo_edit()
            yield await emit("reflect", outcome="verification_failed", rollback=undo.__dict__)
            raise PipelineError(f"Verification failed (exit {verification.exit_code}); edit rolled back")
        yield await emit("reflect", outcome="verified", message="Patch applied and test suite passed")


def _parse_patch(text: str) -> dict:
    """Read the first valid JSON object from a model response, including fenced output."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Model returned no JSON object")
