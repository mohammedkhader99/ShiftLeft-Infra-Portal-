"""Increment 1.5 checks: cost estimation per deployment target."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from db.seed import seed
from db.session import Base


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    seed(session)

    def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()
    session.close()


def _cost(client, target, components):
    return client.post(
        "/api/cost", json={"deployment_target": target, "components": components}
    ).json()


def test_onprem_cost_matches_rates(client):
    # postgres16 medium = 4 vCPU / 16 GB / 200 GB.
    # onprem monthly = 4*45 + 16*12 + 200*1.5 = 180 + 192 + 300 = 672
    # postgres licence = 0; one-time setup = 500.
    body = _cost(client, "onprem", [{"technology_code": "postgres16", "size": "medium"}])
    assert body["totals"]["monthly"] == 672.0
    assert body["totals"]["annual"] == 672.0 * 12
    assert body["totals"]["one_time"] == 500.0


def test_windows_adds_licence_line(client):
    # win2019 small = 2/4/50. onprem resource = 2*45 + 4*12 + 50*1.5 = 90+48+75 = 213
    # windows licence = 320/mo -> monthly = 533.
    body = _cost(client, "onprem", [{"technology_code": "win2019", "size": "small"}])
    line = body["lines"][0]
    assert line["resource_monthly"] == 213.0
    assert line["licence_monthly"] == 320.0
    assert line["monthly"] == 533.0


def test_target_changes_the_price(client):
    comps = [{"technology_code": "postgres16", "size": "small"}]
    onprem = _cost(client, "onprem", comps)["totals"]["monthly"]
    azure = _cost(client, "azure", comps)["totals"]["monthly"]
    oci = _cost(client, "oci", comps)["totals"]["monthly"]
    # Three different targets give three different monthly figures.
    assert len({onprem, azure, oci}) == 3
    # Azure discount (20%) makes it cheaper than OCI here given the seeded rates?
    # Just assert cloud figures are positive and computed.
    assert azure > 0 and oci > 0


def test_azure_discount_is_applied(client):
    # postgres16 small = 2 vCPU / 4 GB / 50 GB.
    # azure monthly = (2*0.14 + 4*0.012)*730*0.8 + 50*0.10*0.8
    #   compute = (0.28 + 0.048)*730 = 239.44 ; *0.8 = 191.552
    #   storage = 5.0 * 0.8 = 4.0 ; total = 195.552 -> rounded 195.55
    body = _cost(client, "azure", [{"technology_code": "postgres16", "size": "small"}])
    assert body["totals"]["monthly"] == pytest.approx(195.55, abs=0.05)


def test_unknown_target_is_zero(client):
    body = _cost(client, "", [{"technology_code": "postgres16", "size": "small"}])
    assert body["known_target"] is False
    assert body["totals"] == {"one_time": 0, "monthly": 0, "annual": 0}


def test_unresolved_component_contributes_zero(client):
    body = _cost(client, "onprem", [{"technology_code": "postgres16", "size": ""}])
    assert body["totals"]["monthly"] == 0
    assert body["lines"][0]["resolved"] is False


# --- Increment 2.1: Azure live pricing adapter -------------------------------


def test_mock_mode_is_default_and_uses_seeded_rates(client):
    # No AZURE_PRICING_MODE set -> mock -> seeded decomposed rate (from 1.5 test).
    body = _cost(client, "azure", [{"technology_code": "postgres16", "size": "small"}])
    assert body["pricing_source"] == "mock"
    assert body["totals"]["monthly"] == pytest.approx(195.55, abs=0.05)


def test_azure_live_uses_adapter_price(client, monkeypatch):
    from api.adapters import azure_pricing

    monkeypatch.setattr(azure_pricing, "is_live", lambda: True)
    # small -> storage 50 GB; cached azure storage rate = 0.10 * 0.8 = 0.08/GB -> 4.0
    # live compute monthly (list) = 100.0; * (1 - 0.20 discount) = 80.0
    monkeypatch.setattr(azure_pricing, "vm_monthly", lambda size, **k: 100.0)
    body = _cost(client, "azure", [{"technology_code": "postgres16", "size": "small"}])
    assert body["pricing_source"] == "azure-live"
    # 80.0 compute + 4.0 storage = 84.0
    assert body["totals"]["monthly"] == pytest.approx(84.0, abs=0.01)


def test_azure_live_falls_back_to_cached_when_unavailable(client, monkeypatch):
    from api.adapters import azure_pricing
    from api.adapters.azure_pricing import AzureUnavailable

    monkeypatch.setattr(azure_pricing, "is_live", lambda: True)

    def boom(size, **k):
        raise AzureUnavailable("down")

    monkeypatch.setattr(azure_pricing, "vm_monthly", boom)
    body = _cost(client, "azure", [{"technology_code": "postgres16", "size": "small"}])
    # Falls back to the cached decomposed rate and flags it.
    assert body["pricing_source"] == "azure-cached"
    assert body["totals"]["monthly"] == pytest.approx(195.55, abs=0.05)


def test_oci_live_uses_adapter_rates(client, monkeypatch):
    from api.adapters import oci_pricing

    monkeypatch.setattr(oci_pricing, "is_live", lambda: True)
    # Raw rates; pricing applies the seeded OCI discount (15%).
    monkeypatch.setattr(
        oci_pricing, "rates",
        lambda: {"vcpu-hour": 0.10, "memory-gb-hour": 0.01, "storage-gb-month": 0.05},
    )
    # small = 2 vCPU / 4 GB / 50 GB.
    # compute = (2*0.10 + 4*0.01)*730 = (0.24)*730 = 175.2 ; storage = 50*0.05 = 2.5
    # -> 177.7 ; * (1 - 0.15) = 151.045 -> 151.05 (rates discounted before formula)
    body = _cost(client, "oci", [{"technology_code": "postgres16", "size": "small"}])
    assert body["pricing_source"] == "oci-live"
    assert body["totals"]["monthly"] == pytest.approx(151.05, abs=0.05)


def test_oci_live_falls_back_when_unavailable(client, monkeypatch):
    from api.adapters import oci_pricing
    from api.adapters.oci_pricing import OCIUnavailable

    monkeypatch.setattr(oci_pricing, "is_live", lambda: True)

    def boom():
        raise OCIUnavailable("down")

    monkeypatch.setattr(oci_pricing, "rates", boom)
    body = _cost(client, "oci", [{"technology_code": "postgres16", "size": "small"}])
    assert body["pricing_source"] == "oci-cached"


def test_oci_mock_is_default(client):
    body = _cost(client, "oci", [{"technology_code": "postgres16", "size": "small"}])
    assert body["pricing_source"] == "mock"
