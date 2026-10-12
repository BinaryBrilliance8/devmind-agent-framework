"""Five-stage, execution-grounded repository engineering pipeline."""
from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

from devmind.agent.llm import LLMClient, LLMError
from devmind.agent.repomap import build_repomap
from devmind.agent.tools import EditError, FileEditor
from devmind.sandbox.executor import Executor


SYSTEM = """You are a repository engineer. Use only the supplied AST map, source excerpts, issue, and test output. If explicit_target_paths is non-empty, you MUST choose one of those exact paths. Do not touch unrelated code. Choose the AST symbol and source excerpt that directly match the issue. Fix application/source code; do not weaken, delete, or rewrite tests/assertions to make verification pass unless the request specifically asks to change tests. Source excerpts contain literal, unnumbered file lines; start_line is separate metadata. For str_replace, copy old text exactly from the excerpt and never add line numbers. Return exactly one JSON object with this schema: {\"reasoning\": string, \"operation\": \"str_replace\"|\"insert\", \"path\": workspace-relative path, \"old\": exact existing text (str_replace only), \"content\": replacement/insert text, \"line\": zero-based insertion line (insert only)}. Use no markdown fences and no extra JSON objects. Make the smallest correct patch. Do not claim tests passed."""


class PipelineError(RuntimeError):
    pass


class AgentRun:
    def __init__(self, workspace: str | Path, client: LLMClient, *, test_command: str = "python3 -m pytest -q", executor=None):
        self.workspace = Path(workspace).resolve()
        self.client = client
        self.executor = executor or Executor(self.workspace)
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
        if _is_visual_request(issue) and not _has_frontend_files(self.workspace):
            raise PipelineError(
                "This request asks for a visual or styling change, but the loaded workspace has no frontend, "
                "template, or stylesheet files. Load the app repository instead of a backend-only benchmark."
            )
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
            yield await emit("reproduce", note="Baseline command passed; continuing to address the request.")
        elif initial.exit_code == 5:
            raise PipelineError(
                "The baseline command collected no tests (exit 5). Configure a real test command before asking the agent to patch this repository."
            )
        elif initial.exit_code != 1:
            raise PipelineError(f"Baseline test command failed to execute (exit {initial.exit_code}): {initial.stderr}")
        # 3. Surgical Patch
        explicit_paths = sorted({symbol["path"] for symbol in symbols if symbol["path"] in issue})
        explicit_symbols = [symbol for symbol in symbols if symbol["path"] in explicit_paths]
        candidate_symbols = explicit_symbols[:8] if explicit_symbols else symbols[:8]
        source_context = []
        context_size = 0
        query_terms = {term for term in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", issue.lower()) if len(term) > 2}
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
            if symbol.get("kind") == "module":
                # Module-level config and inline callbacks are common in JS
                # servers. Include the best matching source windows instead of
                # only the module's first 40 lines.
                ranked_lines = sorted(
                    range(len(lines)),
                    key=lambda i: (-sum(bool(re.search(rf"\b{re.escape(term)}\b", lines[i].lower())) for term in query_terms), i),
                )
                windows: list[tuple[int, int]] = []
                for index in ranked_lines[:12]:
                    if not any(start <= index < end for start, end in windows):
                        windows.append((max(0, index - 7), min(len(lines), index + 9)))
                    if len(windows) >= 4:
                        break
                if not query_terms or not ranked_lines or not any(
                    re.search(rf"\b{re.escape(term)}\b", lines[index].lower())
                    for index in ranked_lines[:12] for term in query_terms
                ):
                    windows = [(0, min(len(lines), 48))]
                context_start = min((start for start, _ in windows), default=0)
                excerpt = "\n…\n".join(
                    "\n".join(lines[start:end])
                    for start, end in sorted(windows)
                )
            else:
                start = max(0, symbol["line"] - 16)
                end = min(len(lines), symbol["end_line"] + 24)
                context_start = start
                excerpt = "\n".join(lines[start:end])
            excerpt = excerpt[:max(0, 30_000 - context_size)]
            source_context.append({"path": symbol["path"], "start_line": context_start + 1, "source": excerpt})
            context_size += len(excerpt)
        prompt = {"issue": issue, "explicit_target_paths": explicit_paths,
                  "ast_symbols": candidate_symbols, "baseline_tests": initial.to_dict(),
                  "source_context": source_context,
                  "instructions": "Return one JSON edit operation. Only target an existing file in this workspace. If explicit_target_paths is non-empty, you MUST edit a path from that list. Source excerpts are literal file text with no line-number prefixes; use exact source text in old when replacing. start_line is reference metadata only."}
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(prompt)}]
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
                if not result.changed:
                    raise EditError(
                        f"The proposed edit made no changes to {patch['path']}. "
                        "Choose a real, issue-relevant change; do not edit tests to hide a failure."
                    )
                if result.changed and hasattr(self.executor, "sync_file"):
                    await self.executor.sync_file(patch["path"])
                yield await emit("tool", tool="file_editor", result=result.__dict__)
            except (ValueError, KeyError, EditError) as exc:
                if attempt:
                    raise PipelineError(f"Patch rejected after one correction attempt: {exc}") from exc
                messages.extend([
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"Your proposed edit was rejected before it changed the file. Error: {exc}\nReturn a corrected JSON edit operation only."},
                ])
                yield await emit("patch_rejected", error=str(exc), correction_attempt=2)
                continue

            # 4. Verify each real edit. Feed a failed run back into one bounded
            # correction attempt after restoring the workspace to its prior state.
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
            if verification.exit_code == 0:
                yield await emit("reflect", outcome="verified", message=f"Patch applied and verification command passed: {self.test_command}")
                return

            undo = await self.editor.undo_edit()
            if attempt:
                yield await emit("reflect", outcome="verification_failed", rollback=undo.__dict__)
                raise PipelineError(f"Verification failed after one correction attempt (exit {verification.exit_code}); latest edit rolled back")
            yield await emit("reflect", outcome="verification_failed_retrying", rollback=undo.__dict__)
            failure_output = "\n".join(part for part in (verification.stdout, verification.stderr) if part)
            messages.extend([
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"Your edit was applied but verification failed (exit {verification.exit_code}), "
                    "so DevMind rolled it back. Diagnose the actual failure output below and return "
                    "a corrected minimal edit to application/source code. Do not weaken or edit the tests.\n\n"
                    f"Verification command: {verification.command}\n"
                    f"Failure output:\n{failure_output[:20_000]}"
                )},
            ])


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


def _is_visual_request(issue: str) -> bool:
    terms = set(re.findall(r"[a-zA-Z]+", issue.lower()))
    return bool(terms & {
        "ui", "interface", "homepage", "pink", "color", "colour",
        "background", "theme", "style", "stylesheet", "css", "font", "layout", "button",
    })


def _has_frontend_files(root: Path) -> bool:
    frontend_suffixes = {".css", ".scss", ".sass", ".less", ".html", ".htm", ".ejs", ".jsx", ".tsx", ".vue", ".svelte", ".astro"}
    ignored = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "coverage"}
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in ignored]
        if any(Path(name).suffix.lower() in frontend_suffixes for name in files):
            return True
        # Python UI frameworks such as Streamlit keep the presentation layer in
        # .py files instead of HTML/CSS templates.
        for name in files:
            if not name.endswith(".py"):
                continue
            try:
                content = (Path(current) / name).read_text(encoding="utf-8")[:200_000]
            except (OSError, UnicodeDecodeError):
                continue
            if re.search(r"^\s*(?:import\s+streamlit\b|from\s+streamlit\b)", content, re.MULTILINE):
                return True
    return False
