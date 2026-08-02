"""AWS pricing adapter + estimate integration (multi-cloud breadth).

AWS is a deployment target priced from the seeded cloud_aws rate cards (mock);
live is a marked extension point that falls back to the cached seeded rates.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import pricing
from api.adapters import aws_pricing
from api.adapters.aws_pricing import AWSUnavailable
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
    monkeypatch.delenv("AWS_PRICING_MODE", raising=False)
    assert aws_pricing.is_live() is False


def test_live_mode_flag(monkeypatch):
    monkeypatch.setenv("AWS_PRICING_MODE", "live")
    assert aws_pricing.is_live() is True


def test_live_rates_is_extension_point():
    with pytest.raises(AWSUnavailable):
        aws_pricing.rates()


# --- Estimate integration ----------------------------------------------------

def test_aws_is_a_known_target():
    assert "aws" in pricing.DEPLOYMENT_TARGETS and pricing.TARGET_KIND["aws"] == "cloud_aws"


def test_aws_estimate_prices_from_seeded_rates(session):
    est = pricing.estimate_cost([{"technology_code": "postgres16", "size": "medium"}], "aws", session)
    assert est["known_target"] is True
    assert est["by_category"]["compute"] > 0 and est["by_category"]["storage"] > 0
    assert est["totals"]["monthly"] > 0
    assert est["pricing_source"] == "mock"


def test_aws_live_falls_back_to_cache(session, monkeypatch):
    monkeypatch.setenv("AWS_PRICING_MODE", "live")
    est = pricing.estimate_cost([{"technology_code": "postgres16", "size": "medium"}], "aws", session)
    # Live isn't wired -> falls back to the seeded rates, flagged.
    assert est["pricing_source"] == "aws-cached" and est["totals"]["monthly"] > 0


def test_validation_accepts_aws_target(session):
    payload = {
        "request_type": "create", "project_code": "EGATE", "cost_centre_code": "IMD-1001",
        "deployment_target": "aws", "environment_name": "egate-aws", "environment_tier": "uat",
        "data_classification": "internal",
        "components": [{"technology_code": "postgres16", "size": "medium"}],
        "business_justification": "Testing AWS as a deployment target for a UAT environment.",
        "priority": "medium", "business_criticality": "tier3", "required_delivery_date": "2027-06-01",
    }
    assert "deployment_target" not in validate_submission(payload, session)
