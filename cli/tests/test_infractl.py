"""infractl CLI checks (F-INT-09): command dispatch, request building, output —
with the HTTP layer mocked."""

import pytest

import cli.infractl as cli


class FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {}

    def json(self):
        return self._data


@pytest.fixture()
def key(monkeypatch):
    monkeypatch.setenv("INFRA_API_KEY", "sk-test")
    monkeypatch.delenv("INFRA_API_URL", raising=False)


def _mock(monkeypatch, resp, capture=None):
    def fake(method, url, **kw):
        if capture is not None:
            capture.append((method, url, kw))
        return resp if not isinstance(resp, list) else resp.pop(0)
    monkeypatch.setattr(cli.httpx, "request", fake)


def test_missing_key_is_an_error(monkeypatch, capsys):
    monkeypatch.delenv("INFRA_API_KEY", raising=False)
    assert cli.main(["whoami"]) == 1
    assert "API key" in capsys.readouterr().err


def test_whoami_builds_request_and_prints(monkeypatch, capsys, key):
    calls = []
    _mock(monkeypatch, FakeResp(200, {"email": "a@x.com", "roles": ["requester", "finops"]}), calls)
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert "a@x.com" in out and "finops" in out
    method, url, kw = calls[0]
    assert method == "GET" and url.endswith("/api/me")
    assert kw["headers"]["X-API-Key"] == "sk-test"


def test_requests_lists(monkeypatch, capsys, key):
    _mock(monkeypatch, FakeResp(200, [
        {"reference": "REQ-1", "status": "provisioned", "environment_name": "e1",
         "estimate": {"monthly": 1200}},
    ]))
    assert cli.main(["requests"]) == 0
    out = capsys.readouterr().out
    assert "REQ-1" in out and "1,200" in out


def test_showback_passes_params(monkeypatch, capsys, key):
    calls = []
    _mock(monkeypatch, FakeResp(200, {
        "group_by": "project", "scope": "committed", "currency": "AED",
        "rows": [{"key": "BIO", "monthly": 500, "count": 4}], "total": {"monthly": 500}}), calls)
    assert cli.main(["showback", "--by", "project", "--scope", "committed"]) == 0
    assert calls[0][2]["params"] == {"group_by": "project", "scope": "committed"}
    assert "BIO" in capsys.readouterr().out


def test_url_override(monkeypatch, key):
    calls = []
    _mock(monkeypatch, FakeResp(200, {"email": "a", "roles": []}), calls)
    cli.main(["--url", "https://portal.example/", "whoami"])
    assert calls[0][1] == "https://portal.example/api/me"  # trailing slash trimmed


def test_api_error_is_surfaced(monkeypatch, capsys, key):
    _mock(monkeypatch, FakeResp(403, {"detail": "not permitted"}))
    assert cli.main(["health"]) == 1
    assert "permitted" in capsys.readouterr().err


def test_transport_error_is_surfaced(monkeypatch, capsys, key):
    def boom(*a, **k):
        raise cli.httpx.ConnectError("refused")
    monkeypatch.setattr(cli.httpx, "request", boom)
    assert cli.main(["whoami"]) == 1
    assert "cannot reach" in capsys.readouterr().err
