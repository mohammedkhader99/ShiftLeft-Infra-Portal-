"""A saved draft can be reopened, and comes back whole (P.11a, F-UX-01).

The form told people "Draft saved as REQ-2026-0001 — you can resume it later"
and the portal had no way to resume anything. The row was in the database the
whole time; the only route back to it was a reference held in React state, which
does not survive leaving the page. F-UX-01 is a Must in ARCHITECTURE.md §10.2,
and half of it — the resume — was never built.

WHAT THESE TESTS ARE ABOUT, in order of how much they matter:

1. A resumed draft carries its PLACEMENT. Placement is not a preference somebody
   can re-express by clicking around again: it decides how many machines get
   built, what they cost, and which modules the orchestrator selects (P.8). A
   draft that came back without it would look complete while having silently
   forgotten the one decision on it that determines what gets built.

2. Saving a resumed draft UPDATES it. The reference goes back with the save, so
   one draft stays one draft. Getting this wrong leaves a requester believing
   they have one request while the database has several, and the first anyone
   notices is the duplicate in an approver's queue.

3. A reference is not a permission. `require_action` authorises the ROLE — may
   this person raise requests at all — and says nothing about whose request this
   one is. That was survivable while references never left the page that minted
   them. Resuming puts them in the URL and in the requests table, which is
   exactly when guessing one starts being worth somebody's time.

4. A submitted request is not a draft. The save path set `status = "draft"`
   unconditionally, so saving against a submitted reference pulled a request back
   out of the approval it was waiting for — silently, while its Jira ticket went
   on existing. The portal would then disagree with Jira about whether the
   request had ever been sent, which is the authority split §4 exists to prevent.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_placement_evaluator, get_session
from api.placement_store import record_placement
from db.models import Request
from db.seed import seed
from db.session import Base

OWNER = "mohammed.khader@emaratechg.ae"  # the mock requester — i.e. "me"
SOMEONE_ELSE = "layla.hassan@emaratechg.ae"


def allow_everything(_topology):
    return {"allow": True, "violations": []}


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as session:
        seed(session)
        yield session


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_placement_evaluator] = lambda: allow_everything
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# A half-finished request, which is the only kind worth saving as a draft. The
# three technologies are deliberate: they are the ones `host_mode_requirement`
# carries rows for, so a layout can actually be sized and priced (see P.11).
HALF_FINISHED = {
    "request_type": "create",
    "project_code": "EGATE",
    "cost_centre_code": "IMD-1001",
    "deployment_target": "oci",
    "environment_name": "egate-uat",
    "environment_tier": "uat",
    "data_classification": "internal",
    "business_justification": "Standing up the UAT estate for the gate release.",
    "priority": "medium",
    "components": [
        {"technology_code": "compute-vm", "size": "small"},
        {"technology_code": "postgres16", "size": "medium"},
        {"technology_code": "nodejs20", "size": "small"},
    ],
}

# The same request with every field submit insists on, for the one test that
# needs a request to actually reach an approver.
SUBMITTABLE = {
    "request_type": "create",
    "project_code": "EGATE",
    "cost_centre_code": "IMD-1001",
    "deployment_target": "onprem",
    "environment_name": "egate-uat",
    "environment_tier": "uat",
    "data_classification": "internal",
    "business_justification": "Needed to run the eGate UAT load tests before go-live.",
    "priority": "high",
    "business_criticality": "tier2",
    "required_delivery_date": (date.today() + timedelta(days=30)).isoformat(),
    "application_owner": "app.owner@emaratechg.ae",
    "technical_owner": "tech.owner@emaratechg.ae",
    "components": [{"technology_code": "postgres16", "size": "medium"}],
}


def save(client, payload, requester=OWNER):
    return client.post("/api/requests/draft", json=payload,
                       headers={"X-Requester": requester})


def resolve(client, ref, option_key, requester=OWNER):
    return client.post("/api/placement/resolve",
                       json={"reference": ref, "option_key": option_key},
                       headers={"X-Requester": requester})


# --- 1. it comes back whole --------------------------------------------------

def test_a_draft_comes_back_with_everything_that_was_typed_into_it(client):
    ref = save(client, HALF_FINISHED).json()["reference"]

    got = client.get(f"/api/requests/{ref}").json()

    assert got["status"] == "draft"
    for field, expected in HALF_FINISHED.items():
        # The tier is the one field the save deliberately rewrites — see the
        # test below, which is about exactly that.
        if field in ("components", "environment_tier"):
            continue
        assert got[field] == expected, f"{field} did not survive the round trip"
    assert [(c["technology_code"], c["size"]) for c in got["components"]] == [
        ("compute-vm", "small"), ("postgres16", "medium"), ("nodejs20", "small")]


def test_the_tier_comes_back_canonical_which_is_why_the_form_translates_it(client):
    """A draft saved as 'uat' comes back as 'UAT', and that is correct.

    `normalise_tier` is liberal in what it accepts and strict in what it stores:
    the tier decides which VCN a request is built in, so "uat" and "UAT" sitting
    side by side in the column would be two tiers as far as a lookup is
    concerned. The consequence lands on whatever resumes the draft — the form's
    dropdown still offers the pre-16-Aug-2026 spellings, so it has to translate
    a stored tier back on the way in or the control is handed a value it cannot
    display and the requester's tier silently reads as blank.

    Asserted here so that if the storage rule ever changes, the test that fails
    is this one, next to the sentence explaining what depends on it.
    """
    ref = save(client, dict(HALF_FINISHED, environment_tier="uat")).json()["reference"]

    assert client.get(f"/api/requests/{ref}").json()["environment_tier"] == "UAT"


def test_a_draft_that_never_existed_is_a_404_not_an_empty_form(client):
    """Loading, refused, and never existed all look like an empty form unless
    the API distinguishes them, and only one of the three is the user's fault."""
    assert client.get("/api/requests/REQ-2026-9999").status_code == 404


# --- 2. the placement comes back with it ------------------------------------

def test_a_resumed_draft_carries_the_layout_it_was_saved_with(client):
    ref = save(client, HALF_FINISHED).json()["reference"]
    chosen = resolve(client, ref, "consolidated")
    assert chosen.status_code == 200, chosen.text

    got = client.get(f"/api/requests/{ref}").json()

    placement = got["placement"]
    assert placement is not None, "the draft came back having forgotten its layout"
    assert placement["option_key"] == "consolidated"
    assert placement["version"] == 1
    # The figure RECORDED with the decision, not one derived again today: rates
    # move, and the number the requester chose against is the one to show back.
    assert placement["estimate"] == chosen.json()["estimate"]
    assert placement["topology"]["hosts"], "a layout with no hosts places nothing"


def test_the_layout_that_comes_back_is_the_one_in_force_not_the_first_one(client):
    """Changing the layout twice leaves the current one on the resumed draft.

    `current_placement` reads the row nothing has superseded rather than the
    highest version, so a withdrawn layout can never be the one that resumes.
    """
    ref = save(client, HALF_FINISHED).json()["reference"]
    for option in ("consolidated", "separated"):
        assert resolve(client, ref, option).status_code == 200

    placement = client.get(f"/api/requests/{ref}").json()["placement"]

    assert placement["option_key"] == "separated"
    assert placement["version"] == 2


def test_a_cluster_layout_names_its_cluster_so_the_browser_need_not_guess(client, db):
    """Only `existing-cluster` deploys onto a cluster, and its id lives on the
    hosts because that is the document the orchestrator selects modules from.

    The API names it. A browser that knew to read `topology.hosts[0].id` for one
    option key and not for the others would be a second place holding that rule,
    and the two would drift.
    """
    ref = save(client, HALF_FINISHED).json()["reference"]
    req = db.scalar(select(Request).where(Request.reference == ref))
    record_placement(
        db, req.id, "existing-cluster",
        {"environment": "uat", "deployment_target": "oci",
         "hosts": [{"id": "ocid1.cluster.oc1..aaaa", "host_mode": "container",
                    "components": ["nodejs20"]}]},
        actor=OWNER)
    db.commit()

    placement = client.get(f"/api/requests/{ref}").json()["placement"]

    assert placement["cluster_id"] == "ocid1.cluster.oc1..aaaa"


def test_a_layout_that_uses_no_cluster_names_none(client):
    ref = save(client, HALF_FINISHED).json()["reference"]
    resolve(client, ref, "consolidated")

    placement = client.get(f"/api/requests/{ref}").json()["placement"]

    assert placement["cluster_id"] is None


def test_a_draft_with_no_layout_says_so_rather_than_inventing_one(client):
    ref = save(client, HALF_FINISHED).json()["reference"]

    assert client.get(f"/api/requests/{ref}").json()["placement"] is None


# --- 3. saving a resumed draft updates it -----------------------------------

def test_saving_a_resumed_draft_updates_it_instead_of_making_another(client, db):
    ref = save(client, HALF_FINISHED).json()["reference"]

    again = save(client, dict(HALF_FINISHED, reference=ref,
                              business_justification="Reworded after a think."))

    assert again.status_code == 200
    assert again.json()["reference"] == ref
    assert db.scalar(select(func.count()).select_from(Request)) == 1
    assert client.get(f"/api/requests/{ref}").json()["business_justification"] == (
        "Reworded after a think.")


# --- 4. a reference is not a permission -------------------------------------

def test_somebody_elses_draft_cannot_be_overwritten_by_naming_its_reference(client):
    ref = save(client, HALF_FINISHED, requester=SOMEONE_ELSE).json()["reference"]

    attempt = save(client, dict(HALF_FINISHED, reference=ref,
                                environment_name="taken-over"), requester=OWNER)

    assert attempt.status_code == 403
    assert SOMEONE_ELSE in attempt.json()["detail"]
    # And it is untouched.
    assert client.get(f"/api/requests/{ref}").json()["environment_name"] == "egate-uat"


# --- 5. a submitted request is not a draft ----------------------------------

def test_a_submitted_request_cannot_be_pulled_back_into_a_draft(client):
    ref = save(client, SUBMITTABLE).json()["reference"]
    submitted = client.post(f"/api/requests/{ref}/submit",
                            headers={"X-Requester": OWNER})
    assert submitted.status_code == 200, submitted.text
    after_submit = client.get(f"/api/requests/{ref}").json()["status"]
    assert after_submit != "draft"

    attempt = save(client, dict(SUBMITTABLE, reference=ref,
                                environment_name="edited-after-approval-asked"))

    assert attempt.status_code == 409
    assert after_submit in attempt.json()["detail"]
    # Still submitted, and still saying what it said when the ticket was raised.
    unchanged = client.get(f"/api/requests/{ref}").json()
    assert unchanged["status"] == after_submit
    assert unchanged["environment_name"] == "egate-uat"
