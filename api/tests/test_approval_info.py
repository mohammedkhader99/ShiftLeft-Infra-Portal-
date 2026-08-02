"""Requester-facing approval info (Portal UI polish / F-GOV-*).

Exposes only the non-sensitive approval facts (where approval happens, how many
approvers + SLA, self-approval blocked, change window) to any signed-in user —
unlike /api/config, which is admin-only and carries the full internal posture.
"""

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)

_SAFE_KEYS = {"system_of_record", "quorum", "sla_hours", "four_eyes", "sod_enforced", "change_window"}


def test_reachable_by_plain_requester(monkeypatch):
    monkeypatch.setenv("ROLE_MAP", '{"req@x.com": ["requester"]}')
    r = client.get("/api/approval-info", headers={"X-Requester": "req@x.com"})
    assert r.status_code == 200  # not admin-gated
    assert set(r.json().keys()) == _SAFE_KEYS


def test_omits_sensitive_posture(monkeypatch):
    body = client.get("/api/approval-info").json()
    # None of the admin-only posture leaks through.
    assert "modes" not in body and "finops" not in body and "provision_mode" not in body
    assert "budget" not in str(body).lower() and "audit_hmac" not in body


def test_reflects_env(monkeypatch):
    monkeypatch.setenv("APPROVAL_QUORUM", "2")
    monkeypatch.setenv("APPROVAL_SLA_HOURS", "12")
    monkeypatch.setenv("FOUR_EYES_ENFORCED", "true")
    monkeypatch.setenv("CHANGE_WINDOW_ENABLED", "true")
    monkeypatch.setenv("CHANGE_WINDOW_DAYS", "mon-fri")
    body = client.get("/api/approval-info").json()
    assert body["quorum"] == 2 and body["sla_hours"] == 12.0 and body["four_eyes"] is True
    assert body["change_window"]["enabled"] is True and body["change_window"]["days"] == "mon-fri"


def test_change_window_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CHANGE_WINDOW_ENABLED", raising=False)
    body = client.get("/api/approval-info").json()
    assert body["change_window"]["enabled"] is False and body["change_window"]["open_now"] is True
