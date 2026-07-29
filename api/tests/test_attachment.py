"""Increment 2.4c checks: the request/costing PDF renders."""

from types import SimpleNamespace

from api.attachment import build_request_pdf

REQ = SimpleNamespace(
    reference="REQ-2026-0001", requester="alice@x.com", request_type="create",
    project_code="EGATE", cost_centre_code="IMD-1001", deployment_target="onprem",
    environment_name="egate-uat", target_environment=None, data_classification="internal",
    # Governance metadata (increment 6.1).
    business_justification="Load testing before go-live.", priority="high",
    business_criticality="tier2", required_delivery_date="2026-12-31",
    application_owner="app@x.com", business_owner=None,
    technical_owner="tech@x.com", environment_owner=None,
)
BREAKDOWN = {
    "currency": "AED", "pricing_source": "mock",
    "lines": [{"monthly": 672.0}, {"monthly": 213.0}],
    "totals": {"one_time": 1000.0, "monthly": 885.0, "annual": 10620.0},
}
SIZING = {"components": [
    {"technology_name": "PostgreSQL 16", "size": "medium", "vcpu": 4, "memory_gb": 16, "storage_gb": 200},
    {"technology_name": "NGINX", "size": "small", "vcpu": 2, "memory_gb": 4, "storage_gb": 50},
]}


def test_pdf_is_generated():
    pdf = build_request_pdf(REQ, BREAKDOWN, SIZING)
    assert isinstance(pdf, bytes)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 500


def test_pdf_handles_empty_components():
    pdf = build_request_pdf(REQ, {"currency": "AED", "lines": [], "totals": {}}, {"components": []})
    assert pdf[:5] == b"%PDF-"
