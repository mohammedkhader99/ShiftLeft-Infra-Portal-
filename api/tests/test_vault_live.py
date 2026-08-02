"""Live HashiCorp Vault credential delivery (F-INT-05 / F-IAM-07).

The security property under test: the portal must never receive the credential.
It asks Vault to wrap the response, so it only ever handles a single-use wrapping
token. These tests mock the HTTP layer so the suite stays offline; the real Vault
proof is a separate, documented verification.
"""

import pytest

from api import vault

_ENV = ("VAULT_MODE", "VAULT_ADDR", "VAULT_TOKEN", "VAULT_NAMESPACE",
        "VAULT_CREDS_PATH", "VAULT_UI_ADDR", "VAULT_VERIFY")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def _live(monkeypatch):
    monkeypatch.setenv("VAULT_MODE", "live")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test:8200")
    monkeypatch.setenv("VAULT_TOKEN", "s.portal-token")


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


# A wrapped credential read. NOTE lease_id is empty — Vault omits it on a wrapped
# response because the lease lives inside the wrapped payload. That is exactly why
# the adapter tracks the child token's accessor instead of a lease id.
_WRAPPED = _Resp(200, {
    "lease_id": "",
    "wrap_info": {"token": "hvs.WRAPPED", "accessor": "wrap-acc", "creation_time": "now"},
})

# The child-token creation response.
_TOKEN = _Resp(200, {"auth": {"client_token": "hvs.CHILD", "accessor": "tok-accessor-1"}})


def _fake_httpx(monkeypatch, get=None, post=None, token=None, captured=None):
    """Fake httpx for the two-step live flow: POST auth/token/create, then GET the
    creds path wrapped. `post` overrides non-token POSTs (revoke)."""
    import types, sys

    def _get(url, **kw):
        if captured is not None:
            captured["get"] = {"url": url, **kw}
        return get if get is not None else _Resp()

    def _post(url, **kw):
        if "auth/token/create" in url:
            if captured is not None:
                captured["create"] = {"url": url, **kw}
            return token if token is not None else _TOKEN
        if captured is not None:
            captured["post"] = {"url": url, **kw}
        return post if post is not None else _Resp()

    mod = types.ModuleType("httpx")
    mod.get, mod.post = _get, _post
    monkeypatch.setitem(sys.modules, "httpx", mod)


# --- Mock mode is untouched and still the default ----------------------------

def test_mock_is_the_default_and_labels_itself():
    out = vault.issue_credential("REQ-1", "u@x.com", "readonly", 8)
    assert out["mock"] is True and "not a real secret" in out["note"]
    assert vault.is_live() is False


# --- Configuration guard -----------------------------------------------------

def test_live_without_config_is_unavailable(monkeypatch):
    monkeypatch.setenv("VAULT_MODE", "live")
    with pytest.raises(vault.VaultUnavailable) as exc:
        vault.issue_credential("REQ-1", "u@x.com", "readonly", 8)
    assert "VAULT_ADDR" in str(exc.value) and "VAULT_TOKEN" in str(exc.value)


# --- The security property: the portal never gets the secret ----------------

def test_issue_asks_vault_to_wrap_and_returns_only_a_link(monkeypatch):
    captured: dict = {}
    _live(monkeypatch)
    _fake_httpx(monkeypatch, get=_WRAPPED, captured=captured)

    out = vault.issue_credential("REQ-1", "u@x.com", "egate-db", 8)
    # A child token was created for the grant, expiring with it.
    assert captured["create"]["json"]["ttl"] == "28800s"
    assert captured["create"]["json"]["explicit_max_ttl"] == "28800s"
    assert captured["create"]["json"]["renewable"] is False
    # The credential was read AS THAT CHILD TOKEN (not the portal's own token),
    # so the lease belongs to it and revoking the token cascades to the lease.
    assert captured["get"]["headers"]["X-Vault-Token"] == "hvs.CHILD"
    assert captured["get"]["headers"]["X-Vault-Wrap-TTL"] == "28800s"
    # It returns a one-time link + the revocable accessor, and no credential.
    assert out["handle"] == "tok-accessor-1"
    assert "hvs.WRAPPED" in out["link"] and out["mock"] is False
    assert "username" not in out and "password" not in out


def test_handle_is_the_token_accessor_not_an_empty_lease(monkeypatch):
    """Regression guard for a real bug: a wrapped response carries lease_id="",
    so tracking the lease id produced a handle that revoked nothing."""
    _live(monkeypatch)
    _fake_httpx(monkeypatch, get=_WRAPPED)
    out = vault.issue_credential("REQ-1", "u@x.com", "egate-db", 8)
    assert out["handle"] == "tok-accessor-1"
    assert out["handle"] not in ("", "wrap-acc")  # not blank, not the wrap accessor


def test_failed_credential_read_revokes_the_child_token(monkeypatch):
    """A grant that can't be completed must not leave a live token behind."""
    captured: dict = {}
    _live(monkeypatch)
    _fake_httpx(monkeypatch, get=_Resp(403), captured=captured)
    with pytest.raises(vault.VaultUnavailable):
        vault.issue_credential("REQ-1", "u@x.com", "egate-db", 8)
    assert captured["post"]["url"].endswith("/v1/auth/token/revoke-accessor")
    assert captured["post"]["json"] == {"accessor": "tok-accessor-1"}


def test_token_creation_failure_is_surfaced(monkeypatch):
    _live(monkeypatch)
    _fake_httpx(monkeypatch, token=_Resp(403))
    with pytest.raises(vault.VaultUnavailable, match="grant token"):
        vault.issue_credential("REQ-1", "u@x.com", "egate-db", 8)


def test_unwrapped_response_is_refused(monkeypatch):
    """If Vault returns the raw secret (no wrap_info), refuse rather than let the
    portal handle a credential."""
    _live(monkeypatch)
    _fake_httpx(monkeypatch, get=_Resp(200, {"data": {"username": "u", "password": "p"}}))
    with pytest.raises(vault.VaultUnavailable, match="Refusing"):
        vault.issue_credential("REQ-1", "u@x.com", "egate-db", 8)


def test_vault_error_is_surfaced_without_leaking(monkeypatch):
    _live(monkeypatch)
    _fake_httpx(monkeypatch, get=_Resp(403, {"errors": ["permission denied"]}))
    with pytest.raises(vault.VaultUnavailable) as exc:
        vault.issue_credential("REQ-1", "u@x.com", "egate-db", 8)
    assert "403" in str(exc.value)


def test_transport_failure_is_wrapped(monkeypatch):
    import types, sys
    mod = types.ModuleType("httpx")

    def _boom(*a, **k):
        raise OSError("connection refused")
    mod.get, mod.post = _boom, _boom
    monkeypatch.setitem(sys.modules, "httpx", mod)
    _live(monkeypatch)
    with pytest.raises(vault.VaultUnavailable, match="Could not reach the vault"):
        vault.issue_credential("REQ-1", "u@x.com", "egate-db", 8)


# --- Path templating (engine-agnostic) ---------------------------------------

def test_default_path_is_dynamic_database_creds():
    assert vault.creds_path("REQ-1", "u@x.com", "egate-db") == "database/creds/egate-db"


def test_path_template_supports_kv_and_placeholders(monkeypatch):
    monkeypatch.setenv("VAULT_CREDS_PATH", "kv/data/envs/{reference}/{scope}")
    assert vault.creds_path("REQ-9", "u@x.com", "ro") == "kv/data/envs/REQ-9/ro"


# --- The unwrap link ---------------------------------------------------------

def test_link_points_at_vault_and_can_use_a_public_address(monkeypatch):
    _live(monkeypatch)
    _fake_httpx(monkeypatch, get=_WRAPPED)
    assert vault.issue_credential("R", "u", "s", 1)["link"].startswith("https://vault.test:8200/ui")
    monkeypatch.setenv("VAULT_UI_ADDR", "https://vault.corp.example")
    assert vault.issue_credential("R", "u", "s", 1)["link"].startswith("https://vault.corp.example/ui")


# --- Revoke ------------------------------------------------------------------

def test_revoke_uses_the_accessor_endpoint_so_leases_cascade(monkeypatch):
    captured: dict = {}
    _live(monkeypatch)
    _fake_httpx(monkeypatch, post=_Resp(204), captured=captured)
    out = vault.revoke("tok-accessor-1")
    assert out["revoked"] is True
    # Revoking the token cascades to every lease it created — the DB login dies.
    assert captured["post"]["url"].endswith("/v1/auth/token/revoke-accessor")
    assert captured["post"]["json"] == {"accessor": "tok-accessor-1"}


def test_revoke_without_a_handle_is_reported_not_raised(monkeypatch):
    _live(monkeypatch)
    _fake_httpx(monkeypatch)
    out = vault.revoke("")
    assert out["revoked"] is False and "No vault handle" in out["note"]


def test_revoke_error_raises(monkeypatch):
    _live(monkeypatch)
    _fake_httpx(monkeypatch, post=_Resp(500))
    with pytest.raises(vault.VaultUnavailable, match="refused to revoke"):
        vault.revoke("some/lease")
