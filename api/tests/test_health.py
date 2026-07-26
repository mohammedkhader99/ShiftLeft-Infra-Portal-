from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health_ok_mock_true(monkeypatch):
    monkeypatch.setenv("USE_MOCK", "true")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "mock": True}


def test_health_mock_false(monkeypatch):
    monkeypatch.setenv("USE_MOCK", "false")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "mock": False}
