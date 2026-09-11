"""Capture a progress fixture from the real endpoint (U.3).

Same rule as refresh_fixtures.py: a fixture invented by hand drifts from what
the API sends, and drift is what the harness exists to catch. This drives a
request through a realistic trail -- approved, agent, handoff, plan, a failed
apply and a retry -- and saves what /api/requests/{ref}/progress answers.

Run from the project root, through stdin so the scratchpad is not on sys.path:

    .venv/Scripts/python.exe - < webapp/frontend/tests/refresh_progress_fixture.py
"""
import json
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
for var in ("AUTH_MODE", "JIRA_MODE", "PROVISION_MODE", "OCI_PRICING_MODE",
            "AZURE_PRICING_MODE", "CLOUD_STATE_MODE", "REGISTRY_MODE"):
    os.environ[var] = "mock"
os.environ["USE_MOCK"] = "true"
os.environ["AUTO_PROVISION"] = "false"
os.environ["ROLE_SOURCE"] = "mock"

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import create_engine               # noqa: E402
from sqlalchemy.orm import sessionmaker            # noqa: E402
from sqlalchemy.pool import StaticPool             # noqa: E402

import api.main as main                            # noqa: E402
from api.audit import append_audit                 # noqa: E402
from db.models import Request                      # noqa: E402
from db.seed import seed                           # noqa: E402
from db.session import Base                        # noqa: E402

OUT = Path("webapp/frontend/tests/fixtures/progress-stalled.json")

#: A request that got as far as a failed apply and was retried. Chosen because
#: it exercises every state the view can draw: done with a time, done WITHOUT
#: one (nothing recorded the plan), current, failed with the reason attached,
#: and steps that have not happened yet.
TRAIL = ("approval.approved", "autobuild.started", "autobuild.finished",
         "orchestrator.handoff", "provisioning.started",
         "apply.failed", "provision.retry")

engine = create_engine("sqlite+pysqlite:///:memory:", future=True,
                       connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
session = sessionmaker(bind=engine, expire_on_commit=False)()
seed(session)

req = Request(reference="REQ-2026-0410", requester="a@b.com", request_type="create",
              status="apply-failed", deployment_target="oci",
              environment_name="fixture", environment_tier="dev",
              status_detail=("Terraform apply failed for oci-instance: the "
                             "service limit for VM.Standard.E4.Flex in "
                             "me-dubai-1 has been reached."))
session.add(req)
session.commit()
for event in TRAIL:
    append_audit(session, event, reference=req.reference, actor="poller")
session.commit()

main.app.dependency_overrides[main.get_session] = lambda: session
with TestClient(main.app) as client:
    body = client.get(f"/api/requests/{req.reference}/progress").json()

OUT.write_text(json.dumps(body["stages"], indent=2) + "\n",
               encoding="utf-8", newline="\n")

print(f"{OUT.name}: {len(body['stages'])} stage(s)")
for s in body["stages"]:
    print(f"    {s['title']:<20} {s['state']:<8} {s['at'] or '(no time recorded)'}")

main.app.dependency_overrides.clear()
session.close()
