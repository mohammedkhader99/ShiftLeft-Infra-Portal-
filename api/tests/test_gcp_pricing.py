"""GCP pricing adapter + estimate integration (multi-cloud breadth).

GCP is a deployment target priced from the seeded cloud_gcp rate cards (mock);
live is a marked extension point that falls back to the cached seeded rates.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import pricing
from api.adapters import gcp_pricing
from api.adapters.gcp_pricing import GCPUnavailable
from api.validation import validate_submission
from db.seed import seed
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    yield s
    s.close()


# --- Adapter -----------------------------------------------------------------

def test_mode_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("GCP_PRICING_MODE", raising=False)
    assert gcp_pricing.is_live() is False


def test_live_mode_flag(monkeypatch):
    monkeypatch.setenv("GCP_PRICING_MODE", "live")
    assert gcp_pricing.is_live() is True


def test_live_rates_is_extension_point():
    with pytest.raises(GCPUnavailable):
        gcp_pricing.rates()


# --- Estimate integration ----------------------------------------------------

def test_gcp_is_a_known_target():
    assert "gcp" in pricing.DEPLOYMENT_TARGETS and pricing.TARGET_KIND["gcp"] == "cloud_gcp"


def test_gcp_estimate_prices_from_seeded_rates(session):
    est = pricing.estimate_cost([{"technology_code": "postgres16", "size": "medium"}], "gcp", session)
    assert est["known_target"] is True
    assert est["by_category"]["compute"] > 0 and est["by_category"]["storage"] > 0
    assert est["totals"]["monthly"] > 0
    assert est["pricing_source"] == "mock"


def test_gcp_live_falls_back_to_cache(session, monkeypatch):
    monkeypatch.setenv("GCP_PRICING_MODE", "live")
    est = pricing.estimate_cost([{"technology_code": "postgres16", "size": "medium"}], "gcp", session)
    assert est["pricing_source"] == "gcp-cached" and est["totals"]["monthly"] > 0


def test_validation_accepts_gcp_target(session):
    payload = {
        "request_type": "create", "project_code": "EGATE", "cost_centre_code": "IMD-1001",
        "deployment_target": "gcp", "environment_name": "egate-gcp", "environment_tier": "uat",
        "data_classification": "internal",
        "components": [{"technology_code": "postgres16", "size": "medium"}],
        "business_justification": "Testing GCP as a deployment target for a UAT environment.",
        "priority": "medium", "business_criticality": "tier3", "required_delivery_date": "2027-06-01",
    }
    assert "deployment_target" not in validate_submission(payload, session)
