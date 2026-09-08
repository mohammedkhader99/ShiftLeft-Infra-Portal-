"""The BFF carries placement to the API and carries the answer back (P.10).

ARCHITECTURE.md §14 decision 10 makes the API the authority and leaves the BFF as
the display layer. That is a claim about what the BFF must NOT do, so most of
these tests assert an absence: it does not filter the options, does not soften a
refusal, does not decide anything.

The reason the absence matters: the API listens on its own port. Anything the
BFF alone enforced would be bypassable by whoever can reach 8081, which inside
this network is everyone. A BFF that filtered placement options would look like
a control while being a decoration, and that is worse than no control at all,
because someone would rely on it.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import webapp.bff.main as bff
from webapp.bff import auth

client = TestClient(bff.app)

# The BFF refuses a POST with no matching Origin (CSRF). Every browser sends
# one; a test client must too, or it is exercising the CSRF guard rather than
# the proxy. There is a test below that the guard is still there.
SAME_ORIGIN = {"Origin": "http://testserver"}


def fake_upstream(captured, body: bytes, status: int = 200):
    class _Upstream:
        content = body
        status_code = status
        headers = {"content-type": "application/json"}

    async def _call(method, url, params, content, headers):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["content"] = content
        return _Upstream()

    return _call


OPTIONS_BODY = json.dumps({
    "reference": "REQ-2026-9001",
    "options": [
        {"key": "managed", "eligible": True, "cheapest": True,
         "totals": {"monthly": 697.13}, "reasons": []},
        {"key": "consolidated", "eligible": False, "cheapest": False,
         "totals": {"monthly": 700.47},
         "reasons": ["postgres16 may not share a host in prod."]},
    ],
}).encode()


# --- it carries the caller's identity ----------------------------------------

def test_placement_options_reaches_the_api_with_the_signed_in_identity(monkeypatch):
    captured = {}
    monkeypatch.setattr(bff, "_proxy_upstream", fake_upstream(captured, OPTIONS_BODY))

    resp = client.post("/api/placement/options",
                       json={"reference": "REQ-2026-9001"}, headers=SAME_ORIGIN)

    assert resp.status_code == 200
    assert captured["url"].endswith("/api/placement/options")
    assert captured["headers"]["X-Requester"] == auth.DEFAULT_DEV_USER["email"]


def test_placement_resolve_forwards_the_body_unchanged(monkeypatch):
    """The API re-decides from this body, so anything the BFF edited on the way
    through would be editing the decision."""
    captured = {}
    monkeypatch.setattr(bff, "_proxy_upstream",
                        fake_upstream(captured, b'{"version":1}'))

    sent = {"reference": "REQ-2026-9001", "option_key": "separated"}
    client.post("/api/placement/resolve", json=sent, headers=SAME_ORIGIN)

    assert json.loads(captured["content"]) == sent
    assert captured["method"] == "POST"


def test_the_identity_is_added_by_the_bff_not_taken_from_the_browser(monkeypatch):
    """A browser claiming to be somebody else must not be believed. The header
    the API reads is the one the BFF wrote from the session."""
    captured = {}
    monkeypatch.setattr(bff, "_proxy_upstream", fake_upstream(captured, OPTIONS_BODY))

    client.post("/api/placement/options",
                json={"reference": "REQ-2026-9001"},
                headers={**SAME_ORIGIN,
                         "X-Requester": "someone.else@example.com"})

    assert captured["headers"]["X-Requester"] == auth.DEFAULT_DEV_USER["email"]


# --- it decides nothing -------------------------------------------------------

def test_the_option_list_is_passed_through_untouched(monkeypatch):
    """Including the option the API refused. A BFF that dropped it would be
    filtering — which is the API's job, and would hide the reason as well."""
    monkeypatch.setattr(bff, "_proxy_upstream", fake_upstream({}, OPTIONS_BODY))

    body = client.post("/api/placement/options",
                       json={"reference": "REQ-2026-9001"}, headers=SAME_ORIGIN).json()

    assert [o["key"] for o in body["options"]] == ["managed", "consolidated"]
    refused = next(o for o in body["options"] if o["key"] == "consolidated")
    assert refused["eligible"] is False
    assert "may not share a host" in refused["reasons"][0]


@pytest.mark.parametrize("status", [400, 403, 409])
def test_a_refusal_reaches_the_browser_with_its_status_and_reason(monkeypatch, status):
    """The BFF must not soften a refusal into a 200 with an empty list, which is
    how a portal comes to look broken instead of strict."""
    detail = b'{"detail":"postgres16 may not share a host in prod."}'
    monkeypatch.setattr(bff, "_proxy_upstream",
                        fake_upstream({}, detail, status=status))

    resp = client.post("/api/placement/resolve",
                       json={"reference": "REQ-2026-9001",
                             "option_key": "consolidated"},
                       headers=SAME_ORIGIN)

    assert resp.status_code == status
    assert "may not share a host" in resp.json()["detail"]


def test_the_bff_has_no_placement_route_of_its_own():
    """Placement travels the generic /api proxy. A dedicated BFF route would be
    a place for logic to accumulate, and logic here is unenforceable."""
    paths = {getattr(r, "path", "") for r in bff.app.routes}
    assert not any("placement" in p for p in paths)


def test_a_cross_origin_post_is_still_refused(monkeypatch):
    """The CSRF guard the tests above satisfy is real, and placement is not
    exempt from it. Without this, adding SAME_ORIGIN everywhere would have
    quietly hidden a hole rather than worked around a rule."""
    monkeypatch.setattr(bff, "_proxy_upstream", fake_upstream({}, OPTIONS_BODY))

    resp = client.post("/api/placement/resolve",
                       json={"reference": "REQ-2026-9001", "option_key": "managed"},
                       headers={"Origin": "https://evil.example"})

    assert resp.status_code == 403
