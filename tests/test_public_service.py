from __future__ import annotations

from fastapi.testclient import TestClient

from devmind import server


def test_visitors_receive_isolated_workspaces(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "PUBLIC_MODE", False)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    server.STATE.clear()
    first, second = TestClient(server.app), TestClient(server.app)
    left = first.post("/api/workspace", json={"benchmark": "async-rate-limiter"})
    right = second.post("/api/workspace", json={"benchmark": "async-rate-limiter"})
    assert left.status_code == right.status_code == 200
    assert left.json()["path"] != right.json()["path"]
    assert left.json()["test_command"] == "python3 -m pytest -q"
    assert left.json()["origin"] == "Benchmark: async-rate-limiter"
    assert first.get("/api/tree").json()["path"] == left.json()["path"]
    assert second.get("/api/tree").json()["path"] == right.json()["path"]
    server.STATE.clear()


def test_public_mode_rejects_non_github_sources_before_ingestion(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "PUBLIC_MODE", True)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    server.STATE.clear()
    response = TestClient(server.app).post(
        "/api/workspace", json={"source": "https://attacker.example/owner/repo"}
    )
    assert response.status_code == 400
    assert "only https://github.com" in response.json()["detail"]
    server.STATE.clear()


def test_run_endpoint_passes_selected_test_command_to_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "PUBLIC_MODE", False)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    server.STATE.clear()
    captured = {}

    class RecordingAgent:
        def __init__(self, workspace, client, *, test_command, executor):
            captured["test_command"] = test_command

        async def events(self, issue):
            yield {"stage": "reflect", "outcome": "verified"}

    monkeypatch.setattr(server, "AgentRun", RecordingAgent)
    client = TestClient(server.app)
    workspace = client.post("/api/workspace", json={"benchmark": "async-rate-limiter"})
    assert workspace.status_code == 200
    response = client.post("/api/run", json={
        "issue": "Run this repository with Node's test runner",
        "provider": "ollama",
        "test_command": "node --test",
    })
    assert response.status_code == 200
    assert '"outcome": "verified"' in response.text
    assert captured["test_command"] == "node --test"
    server.STATE.clear()


def test_workspace_does_not_recommend_placeholder_package_test_script(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}',
        encoding="utf-8",
    )

    assert server._detect_test_command(root) is None


def test_workspace_detects_declared_javascript_test_suite(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    (root / "src.test.js").write_text("", encoding="utf-8")

    assert server._detect_test_command(root) == "npm test"


def test_agent_endpoint_requires_verification_when_workspace_has_no_tests(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "PUBLIC_MODE", False)
    monkeypatch.setattr(server, "current_workspace", lambda request: tmp_path)
    response = TestClient(server.app).post("/api/run", json={
        "issue": "Make a focused repository change",
        "provider": "ollama",
    })

    assert response.status_code == 422
    assert "No test command or test suite" in response.json()["detail"]
