"""Recapture the render-harness fixtures from the real endpoint.

The harness docstring says to capture a real answer rather than editing these by
hand, because a fixture invented by hand drifts from what the API sends -- and
drift is exactly what the harness exists to catch. This is that capture, run
in-process against a seeded database and the REAL OPA so the refusals are the
policy's own sentences, not ones written here.

Why it was needed: `route` was added to every option (U.1) and the fixtures
predate it, so the workspace grouped nothing and the harness passed on a
render that showed no route cards at all.
"""
import json
import os
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
for var in ("AUTH_MODE", "JIRA_MODE", "PROVISION_MODE", "OCI_PRICING_MODE",
            "AZURE_PRICING_MODE", "CLOUD_STATE_MODE", "REGISTRY_MODE"):
    os.environ[var] = "mock"
os.environ["USE_MOCK"] = "true"
# PINNED ON, THOUGH THE PRODUCT DEFAULT IS OFF SINCE 2026-09-12.
#
# The cluster fixture exists to exercise the workspace's grouping and rendering
# WITH a Kubernetes route present — two routes, a recommendation in each, a
# discovered cluster, alternatives behind a disclosure. Capturing it at whatever
# the default happens to be would silently swap that coverage for a screen of
# refusals the moment the default moved, which is exactly what happened to the
# `route` field and left the harness passing on a render with no route cards at
# all.
os.environ["CLUSTER_DEPLOYMENT_ENABLED"] = "true"
os.environ["AUTO_PROVISION"] = "false"
os.environ["ROLE_SOURCE"] = "mock"

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import create_engine               # noqa: E402
from sqlalchemy.orm import sessionmaker            # noqa: E402
from sqlalchemy.pool import StaticPool             # noqa: E402

import api.main as main                            # noqa: E402
from db.models import Blueprint                    # noqa: E402
from db.seed import seed                           # noqa: E402
from db.session import Base                        # noqa: E402

OUT = Path("webapp/frontend/tests/fixtures")

SCENARIOS = {
    # The two the harness already described, unchanged in substance so the
    # checks written against them still mean what they meant.
    "options-prod": {
        "tier": "prod",
        "components": [
            {"technology_code": "mysql", "size": "medium"},
            {"technology_code": "postgres16", "size": "medium"},
        ],
    },
    "options-cluster": {
        "tier": "dev",
        "components": [
            {"technology_code": "oci-oke", "size": "medium"},
            {"technology_code": "nodejs20", "size": "small"},
            {"technology_code": "postgres16", "size": "medium"},
        ],
    },
}


def capture(name: str, spec: dict) -> list:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    for c in spec["components"]:
        code = c["technology_code"]
        # WHICH COMPONENT *IS* A CLUSTER is read from the blueprint's resource
        # kind (api/main._component_facts), so oci-oke has to carry oci-oke here
        # or the capture produces no Kubernetes route at all -- which is exactly
        # what the first attempt did.
        session.add(Blueprint(technology_code=code, deployment_target="oci",
                              status="certified",
                              resource_kind="oci-oke" if code == "oci-oke" else "oci-instance",
                              blueprint_ref=f"oci/{code}"))
    session.commit()

    main.app.dependency_overrides[main.get_session] = lambda: session
    try:
        with TestClient(main.app) as client:
            draft = {
                "request_type": "create", "project_code": "EGATE",
                "cost_centre_code": "IMD-1001", "deployment_target": "oci",
                "environment_name": f"fixture-{name}",
                "environment_tier": spec["tier"],
                "data_classification": "internal",
                "components": spec["components"],
                "business_justification": "Captured for the render harness fixtures.",
                "priority": "high", "business_criticality": "tier2",
                "required_delivery_date": (date.today() + timedelta(days=30)).isoformat(),
                "application_owner": "app.owner@emaratechg.ae",
                "technical_owner": "tech.owner@emaratechg.ae",
            }
            r = client.post("/api/requests/draft", json=draft)
            r.raise_for_status()
            reference = r.json()["reference"]

            r = client.post("/api/placement/options", json={"reference": reference})
            r.raise_for_status()
            return r.json()["options"]
    finally:
        main.app.dependency_overrides.clear()
        session.close()


for name, spec in SCENARIOS.items():
    options = capture(name, spec)
    (OUT / f"{name}.json").write_text(
        json.dumps(options, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{name}: {len(options)} option(s)")
    for o in options:
        print(f"    {o['key']:18} route={o['route']:11} "
              f"eligible={o['eligible']!s:5} resolved={o['resolved']!s:5} "
              f"{'' if o['eligible'] else (o['reasons'][:1] or [''])[0][:60]}")
