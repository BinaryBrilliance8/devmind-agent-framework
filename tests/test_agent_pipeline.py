import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from devmind.agent.core import AgentRun, PipelineError, _has_frontend_files
from devmind.agent.llm import LLMEvent


def init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "--local", "user.name", "DevMind"], cwd=root, check=True)
    subprocess.run(["git", "config", "--local", "user.email", "bot@devmind.local"], cwd=root, check=True)


class CorrectingClient:
    def __init__(self, patch: dict):
        self.patch = patch
        self.called = False
        self.messages = None

    async def stream(self, messages):
        self.called = True
        self.messages = messages
        yield LLMEvent("token", {"text": json.dumps(self.patch)})
        yield LLMEvent("complete", {
            "provider": "test-double",
            "model": "deterministic-patch-fixture",
            "usage": {"prompt_tokens": 21, "completion_tokens": 13, "total_tokens": 34},
            "latency_ms": 0.1,
        })


class SequenceClient:
    def __init__(self, *patches: dict):
        self.patches = list(patches)
        self.messages_seen = []

    async def stream(self, messages):
        self.messages_seen.append(messages)
        patch = self.patches.pop(0)
        yield LLMEvent("token", {"text": json.dumps(patch)})
        yield LLMEvent("complete", {
            "provider": "test-double",
            "model": "deterministic-patch-fixture",
            "usage": {"prompt_tokens": 21, "completion_tokens": 13, "total_tokens": 34},
            "latency_ms": 0.1,
        })


def test_agent_pipeline_runs_real_tests_applies_patch_and_reports_verified(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_repo(root)
    (root / "calculator.py").write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_addition():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    client = CorrectingClient({
        "reasoning": "The function subtracts instead of adding.",
        "operation": "str_replace",
        "path": "calculator.py",
        "old": "return left - right",
        "content": "return left + right",
    })

    events = asyncio.run(_collect(AgentRun(root, client).events("Fix calculator.py add to sum its inputs")))

    assert client.called
    assert events[-1]["stage"] == "reflect"
    assert events[-1]["outcome"] == "verified"
    assert (root / "calculator.py").read_text(encoding="utf-8").endswith("return left + right\n")
    assert next(event for event in events if event["stage"] == "reproduce")["command"]["exit_code"] == 1
    assert next(event for event in events if event["stage"] == "verify")["command"]["exit_code"] == 0


def test_agent_fails_before_model_call_when_baseline_collects_no_tests(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_repo(root)
    client = CorrectingClient({})

    with pytest.raises(PipelineError, match="collected no tests"):
        asyncio.run(_collect(AgentRun(root, client).events("Fix the bug in the repository")))
    assert not client.called


def test_agent_includes_ranked_javascript_module_context_in_prompt(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_repo(root)
    lines = [f"const fixture_{index} = {index};" for index in range(1, 45)]
    lines[32] = 'store.on("error", () => console.log(err));'
    (root / "app.js").write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    client = CorrectingClient({
        "reasoning": "The event handler must accept the error argument.",
        "operation": "str_replace",
        "path": "app.js",
        "old": 'store.on("error", () => console.log(err));',
        "content": 'store.on("error", (err) => console.error(err));',
    })

    events = asyncio.run(_collect(AgentRun(root, client, test_command="python3 -c pass").events(
        "Fix the error callback in app.js so its err variable is defined"
    )))

    prompt = json.loads(client.messages[-1]["content"])
    context = next(entry["source"] for entry in prompt["source_context"] if entry["path"] == "app.js")
    assert 'store.on("error", () => console.log(err));' in context
    assert '33: store.on("error", () => console.log(err));' not in context
    assert events[-1]["stage"] == "reflect" and events[-1]["outcome"] == "verified"


def test_agent_retries_noop_edit_then_applies_corrected_patch(tmp_path: Path) -> None:
    root = _calculator_repo(tmp_path)
    client = SequenceClient(
        {
            "reasoning": "Replace the return with itself.",
            "operation": "str_replace",
            "path": "calculator.py",
            "old": "return left - right",
            "content": "return left - right",
        },
        {
            "reasoning": "The function subtracts instead of adding.",
            "operation": "str_replace",
            "path": "calculator.py",
            "old": "return left - right",
            "content": "return left + right",
        },
    )

    events = asyncio.run(_collect(AgentRun(root, client).events("Fix calculator.py add to sum its inputs")))

    assert len(client.messages_seen) == 2
    assert "made no changes" in client.messages_seen[1][-1]["content"]
    assert (root / "calculator.py").read_text(encoding="utf-8").endswith("return left + right\n")
    assert events[-1]["stage"] == "reflect" and events[-1]["outcome"] == "verified"


def test_repeated_noop_edit_fails_without_verification_or_rollback(tmp_path: Path) -> None:
    root = _calculator_repo(tmp_path)
    noop = {
        "reasoning": "This is the same assertion.",
        "operation": "str_replace",
        "path": "calculator.py",
        "old": "return left - right",
        "content": "return left - right",
    }
    client = SequenceClient(noop, noop)

    with pytest.raises(PipelineError, match="Patch rejected after one correction attempt.*made no changes"):
        asyncio.run(_collect(AgentRun(root, client).events("Fix calculator.py add to sum its inputs")))

    assert len(client.messages_seen) == 2
    assert (root / "calculator.py").read_text(encoding="utf-8").endswith("return left - right\n")


def test_agent_feeds_failed_verification_back_for_one_corrective_edit(tmp_path: Path) -> None:
    root = _calculator_repo(tmp_path)
    client = SequenceClient(
        {
            "reasoning": "Change the implementation.",
            "operation": "str_replace",
            "path": "calculator.py",
            "old": "return left - right",
            "content": "return left * right",
        },
        {
            "reasoning": "The test output shows addition is required.",
            "operation": "str_replace",
            "path": "calculator.py",
            "old": "return left - right",
            "content": "return left + right",
        },
    )

    events = asyncio.run(_collect(AgentRun(root, client).events("Fix calculator.py add to sum its inputs")))

    assert len(client.messages_seen) == 2
    assert "verification failed (exit 1)" in client.messages_seen[1][-1]["content"]
    assert "AssertionError" in client.messages_seen[1][-1]["content"]
    assert any(event.get("outcome") == "verification_failed_retrying" for event in events)
    assert events[-1]["stage"] == "reflect" and events[-1]["outcome"] == "verified"
    assert (root / "calculator.py").read_text(encoding="utf-8").endswith("return left + right\n")


def test_visual_request_on_backend_only_workspace_stops_before_tests_and_model(tmp_path: Path) -> None:
    root = _calculator_repo(tmp_path)
    client = CorrectingClient({})

    with pytest.raises(PipelineError, match="no frontend, template, or stylesheet files"):
        asyncio.run(_collect(AgentRun(root, client).events("Make the homepage UI pink")))

    assert not client.called


def test_streamlit_python_files_count_as_frontend(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('Home')\n", encoding="utf-8")

    assert _has_frontend_files(tmp_path)


def _calculator_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    init_repo(root)
    (root / "calculator.py").write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_addition():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    return root


async def _collect(events):
    return [event async for event in events]
