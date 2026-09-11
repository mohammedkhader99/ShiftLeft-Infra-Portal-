"""The HTTP client is built once, and a call site keeps its own timeout.

Why this is worth a test: the whole point of the shared client is that the
expensive part — the connection pool and the SSL context — is built once. If
someone later "simplifies" it back to building one per call, everything still
passes and nothing looks wrong; the system just gets slower in a way only a
stopwatch can see. These tests fail instead.
"""

import httpx
import pytest

from common import httpclient


@pytest.fixture(autouse=True)
def _fresh():
    """Each test starts and finishes without a client, so none leaks between
    them and none is left holding sockets afterwards."""
    httpclient.close()
    yield
    httpclient.close()


def test_the_same_client_answers_every_time():
    assert httpclient.client() is httpclient.client()


def test_closing_it_means_the_next_call_gets_a_fresh_one():
    first = httpclient.client()
    httpclient.close()
    assert httpclient.client() is not first


def test_a_call_site_keeps_its_own_timeout(monkeypatch):
    """The shared client must never become the place where one caller's patience
    silently becomes another's. OPA on loopback wants 5s; a cloud price list
    wants 8s; both pass their own and both must get it."""
    client = httpclient.client()
    seen = {}

    def fake_send(request, **kw):
        seen.update(request.extensions.get("timeout") or {})
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(client, "send", fake_send)
    client.post("http://example.invalid/x", json={}, timeout=1.25)
    assert seen.get("connect") == 1.25, seen
    assert seen.get("read") == 1.25, seen


def test_the_policy_gate_does_not_build_its_own_client():
    """A guard, not a style rule. The policy gate is the hottest caller in the
    system — every submit, every placement evaluation, every orchestrator
    re-check — so it is the one place where reverting to `httpx.post` costs the
    most and shows the least."""
    import inspect

    import api.policy as policy
    import orchestrator.main as orchestrator

    for module in (policy, orchestrator):
        source = inspect.getsource(module)
        assert "httpx." not in source, (
            f"{module.__name__} builds a client per call again; "
            "use common.httpclient.client()")
