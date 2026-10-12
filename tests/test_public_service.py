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
