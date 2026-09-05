"""How a technology is delivered is a fact about it, not about its name.

The agent inferred this from the CODE: anything not prefixed `oci-`, `aws-`,
`azure-` or `gcp-` was assumed to be software you install on a machine. A naming
convention doing a domain model's job, and wrong in both directions:

  * `postgres16` is OCI's MANAGED database service. The rule read it as software
    on a VM, which is only harmless today because a shipped oci-postgres recipe
    exists and the classifier is never consulted. Decertify it — as apache was
    decertified this afternoon — and the agent would draft a VM profile and
    prove the wrong thing.

  * "Backup & Recovery" is an outcome nobody can install. The rule read it as
    software, the agent guessed `dnf install backup`, and REQ-2026-0183 booted a
    real machine to be told it does not exist. Five minutes and a VM for
    something the catalogue could have said for nothing.

Six entries are in that second category: backup, logging, monitoring,
api-gateway, service-mesh and k8s.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_blueprint as ab
from api import autobuild
from db.seed import seed
from db.session import Base


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUTOBUILD_ENABLED", "true")
    monkeypatch.setenv("AI_MODE", "mock")


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.commit()
    yield s
    s.close()


# --- the two the naming rule got wrong ---------------------------------------

def test_managed_postgres_is_not_read_as_software_on_a_vm(db):
    """THE latent one. `postgres16` provisions OCI's managed database service —
    that is why it prices at 227 rather than 90 — and the old rule called it
    software because the code has no cloud prefix."""
    assert ab.delivery_model("postgres16", db) == "managed"
    assert ab.classify("postgres16", db)[0] == "new-service"


@pytest.mark.parametrize("code", ["backup", "logging", "monitoring",
                                  "api-gateway", "service-mesh", "k8s"])
def test_a_capability_is_recognised_as_one(code):
    """None of these has a package, an archive or a cloud resource behind it."""
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.commit()
    assert ab.delivery_model(code, s) == "capability"
    assert ab.classify(code, s)[0] == "capability"
    s.close()


# --- and the ones it got right stay right ------------------------------------

@pytest.mark.parametrize("code,expected", [
    ("oci-objectstorage", "managed"), ("oci-oke", "managed"),
    ("oci-adb", "managed"), ("aws-s3", "managed"), ("gcp-gke", "managed"),
    ("compute-vm", "machine"), ("rhel9", "machine"), ("win2019", "machine"),
])
def test_the_rest_of_the_catalogue_is_recorded(db, code, expected):
    assert ab.delivery_model(code, db) == expected


@pytest.mark.parametrize("code", ["a-technology-nobody-has-added-yet"])
def test_software_falls_through_to_the_guess_and_still_works(db, code):
    """An unrecorded technology must keep working — one added tomorrow is
    guessed at rather than known, and `delivery_model` says which.

    RE-POINTED 2026-08-30. This listed `nginx` and `kafka`, which are now
    RECORDED: every technology a blueprint builds was classified so that the
    blueprint-versus-catalogue drift guard could see it (a blueprint that started
    calling Kafka a managed service previously slipped past). The property here
    is unchanged and still worth holding — it is about technologies nobody has
    classified, and the examples had stopped being examples of that.

    RE-POINTED AGAIN 2026-09-05, and this time that day arrived: `keycloak`
    and `oracle-db` were the last two real examples, and classifying the
    final eight left only the invented code -- which is what the closing
    sentence of this docstring was written for.

    Recording them changed no answer, which was measured rather than assumed:
    `software` and `machine` both resolve to `vm-service`, the same verdict the
    guess produced for all seven. The invented code keeps this test honest even
    if every real technology is classified one day."""
    assert ab.delivery_model(code, db) == ""
    assert ab.classify(code, db)[0] == "vm-service"


@pytest.mark.parametrize("code", ["nginx", "kafka", "redis7", "python312"])
def test_recording_a_technology_gives_the_same_answer_as_the_guess(db, code):
    """The classification added knowledge; it must not have changed behaviour.

    Four of the seven are VERSIONED codes, which take a different branch in
    `classify` — the version-bump path — when nothing is recorded for them. So
    "it obviously cannot change anything" was not good enough."""
    assert ab.delivery_model(code, db) == "software"
    assert ab.classify(code, db)[0] == "vm-service"


# --- what it saves -----------------------------------------------------------

def test_a_capability_is_refused_without_drafting_or_building(db):
    """REQ-2026-0183 spent a real machine on `dnf install backup`. The recipe
    memory made that cost one machine instead of one per request; this makes it
    cost none."""
    proved, published = [], []

    result = autobuild.ensure(
        "backup", db, target="oci", shipped=lambda c: None,
        run_proof=lambda s, m: proved.append(1),
        publish=published.append, certify=lambda m, r: None,
        withdraw=lambda f: None)

    assert result.status == "refused"
    assert proved == [], "it booted a machine for something with nothing to install"
    assert published == [], "it published a recipe for an outcome"


def test_the_refusal_explains_and_points_somewhere(db):
    """A refusal that only says no teaches nobody anything. A requester who asked
    for "Service Mesh" should learn what to ask for instead."""
    result = autobuild.ensure(
        "service-mesh", db, target="oci", shipped=lambda c: None,
        run_proof=lambda s, m: None, publish=lambda f: None,
        certify=lambda m, r: None, withdraw=lambda f: None)

    assert "Kubernetes" in result.detail
    assert "oke" in result.detail.lower(), "it did not say what to request instead"
    assert "no machine was spent" in result.detail


def test_generic_kubernetes_points_at_the_real_service(db):
    """`k8s` and `oci-oke` both say Kubernetes; only one builds anything."""
    result = autobuild.ensure(
        "k8s", db, target="oci", shipped=lambda c: None,
        run_proof=lambda s, m: None, publish=lambda f: None,
        certify=lambda m, r: None, withdraw=lambda f: None)
    assert "oci-oke" in result.detail


def test_software_is_still_built_normally(db):
    """The catalogue must not become a gate on everything else."""
    from api.proof import ProofOutcome
    proved = []

    autobuild.ensure(
        "kafka", db, target="oci",
        shipped=lambda c: {"ref": "oci/service-vm", "target": "oci",
                           "resource_kind": "oci-service-vm"},
        run_proof=lambda s, m: (proved.append(1)
                                or ProofOutcome("P", "passed", "built")),
        publish=lambda f: None, certify=lambda m, r: None, withdraw=lambda f: None)

    assert proved == [1], "a normal software component was refused"


# --- the grouping ------------------------------------------------------------

def test_the_catalogue_groups_by_how_things_are_delivered(db):
    """One source for the grouping a requester sees and the decision the agent
    makes. They were two, and the two disagreed about postgres16."""
    from fastapi.testclient import TestClient

    from api.main import app, get_session

    def override():
        yield db
    app.dependency_overrides[get_session] = override
    try:
        body = TestClient(app).get("/api/catalogue/delivery?target=oci").json()
    finally:
        app.dependency_overrides.clear()

    codes = {m: {e["code"] for e in items} for m, items in body["groups"].items()}
    assert "postgres16" in codes["managed"]
    assert "oci-objectstorage" in codes["managed"]
    assert {"compute-vm", "rhel9", "win2019"} <= codes["machine"]
    assert {"backup", "logging", "monitoring"} <= codes["capability"]
    # CLASSIFIED 2026-09-05. This asserted `keycloak` was UNCLASSIFIED, which was
    # right at the time and is the weaker half of the property: the view must
    # report what it does not know rather than guess at it. What it must ALSO do
    # is leave nothing unknown once the catalogue records everything -- and six
    # CERTIFIED technologies were reaching the request form with no group at all.
    assert "keycloak" in codes["software"]
    assert body["unclassified"] == [], (
        f"the form would show these with no group: "
        f"{[e['code'] for e in body['unclassified']]}")


def test_the_grouped_view_says_which_are_guesses(db):
    from fastapi.testclient import TestClient

    from api.main import app, get_session

    def override():
        yield db
    app.dependency_overrides[get_session] = override
    try:
        body = TestClient(app).get("/api/catalogue/delivery?target=oci").json()
    finally:
        app.dependency_overrides.clear()

    for entry in body["unclassified"]:
        assert entry["guessed_as"], (
            "an unclassified entry must say what it is being guessed as, or the "
            "catalogue looks complete when it is not")
