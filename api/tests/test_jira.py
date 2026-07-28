"""Increment 2.4 checks: live Jira Server/DC create + status mapping.

The real Jira is never called — httpx is mocked. Mock-mode behaviour is covered
by the request tests.
"""

import pytest

from api import jira
from api.jira import JiraError
from db.models import Request


@pytest.fixture(autouse=True)
def live_env(monkeypatch):
    monkeypatch.setenv("JIRA_MODE", "live")
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_PAT", "test-pat")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "INFRA")
    monkeypatch.setenv("JIRA_ISSUE_TYPE", "Service Request")
    yield


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 300:
            raise RuntimeError(self.text)

    def json(self):
        return self._payload


def _request():
    return Request(reference="REQ-2026-0001", requester="alice@x.com",
                   request_type="create", components=[])


def test_live_create_returns_real_key(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["auth"] = headers.get("Authorization")
        return _Resp({"key": "INFRA-42"}, status=201)

    monkeypatch.setattr(jira.httpx, "post", fake_post)
    approval = jira.create_issue(session=None, req=_request(), body="body text")
    assert approval.jira_key == "INFRA-42"
    assert approval.ticket_url == "https://jira.example.com/browse/INFRA-42"
    # Bearer PAT auth + reporter set to the requester.
    assert captured["auth"] == "Bearer test-pat"
    assert captured["json"]["fields"]["reporter"] == {"name": "alice@x.com"}
    assert captured["url"].endswith("/rest/api/2/issue")


def test_extra_fields_are_merged(monkeypatch):
    monkeypatch.setenv(
        "JIRA_EXTRA_FIELDS",
        '{"customfield_13657": {"value": "Non-Production"}, '
        '"customfield_14503": ["svc (SD-1)"]}',
    )
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["fields"] = json["fields"]
        return _Resp({"key": "SDIMD-1"}, status=201)

    monkeypatch.setattr(jira.httpx, "post", fake_post)
    jira.create_issue(session=None, req=_request(), body="body")
    assert captured["fields"]["customfield_13657"] == {"value": "Non-Production"}
    assert captured["fields"]["customfield_14503"] == ["svc (SD-1)"]


def test_bad_extra_fields_json_raises(monkeypatch):
    monkeypatch.setenv("JIRA_EXTRA_FIELDS", "{not valid json")
    monkeypatch.setattr(jira.httpx, "post", lambda *a, **k: _Resp({"key": "X-1"}, 201))
    with pytest.raises(JiraError):
        jira.create_issue(session=None, req=_request(), body="body")


def test_live_create_failure_raises(monkeypatch):
    monkeypatch.setattr(jira.httpx, "post",
                        lambda *a, **k: _Resp({}, status=403, text="Forbidden"))
    with pytest.raises(JiraError):
        jira.create_issue(session=None, req=_request(), body="body")


@pytest.mark.parametrize(
    "name,expected",
    [("Approved", "approved"), ("Done", "approved"),
     ("Rejected", "rejected"), ("Cancelled", "rejected"),
     ("In Progress", "pending"), ("Waiting for approval", "pending")],
)
def test_status_mapping(monkeypatch, name, expected):
    monkeypatch.setattr(
        jira.httpx, "get",
        lambda *a, **k: _Resp({"fields": {"status": {"name": name}}}),
    )
    assert jira.get_status("INFRA-42") == expected


def test_get_status_unreachable_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(jira.httpx, "get", boom)
    with pytest.raises(JiraError):
        jira.get_status("INFRA-42")
