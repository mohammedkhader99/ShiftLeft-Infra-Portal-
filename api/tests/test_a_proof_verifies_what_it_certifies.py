"""REQ-2026-0232: a proof passed in 120 seconds without building anything.

Elasticsearch was CERTIFIED on evidence that does not exist. No compute instance
was ever created, no boot report was ever filed, and the whole proof took two
minutes where a real one takes four hundred to six hundred seconds.

THE PROOF ASKED ABOUT A RESOURCE KIND THAT DOES NOT EXIST.

Its ladder came back EMPTY — the repositories do not carry Elasticsearch, no
vendor repository or archive is curated for it, and `registry.find` looks only
for the `latest` tag, which Docker Hub's deprecated image no longer publishes.
So the install loop never ran and control fell through to `build`, which drafts
TERRAFORM. With no manifest, `run_proof` invents a kind from the code:

    resource_kind = (manifest or {}).get("resource_kind", f"{target}-{code}")

`oci-elasticsearch` is claimed by no blueprint, so the orchestrator answered
"not a machine — nothing boots, so nothing can report", `checked` came back 0,
and verify read that silence as success:

    "nothing here files a report; it built and tore down"

The certificate was then issued against `oci-service-vm` — a kind that IS a
machine and DOES owe a report. The proof and the certification were not even
describing the same resource.

REQ-2026-0232 then built a real machine, which said `elasticsearch NOT
INSTALLED`. The delivery path was honest; only certification was not.

THE LADDER IS THE ONLY THING THAT INSTALLS SOFTWARE ONTO A MACHINE. If it
offered nothing, the honest answer is that there is no way to install this —
said plainly, with what would change it, and costing no machine at all.

Nothing here reaches the network or a machine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import autobuild
from api.proof import ProofOutcome
from db.seed import seed
from db.session import Base

SERVICE_VM = {
    "ref": "oci/service-vm", "target": "oci",
    "resource_kind": "oci-service-vm", "builds": [], "version": "1.0.0",
}

IMAGE = {"image": "docker.io/library/thing", "tag": "latest",
         "digest": "sha256:" + "a" * 64, "ports": [9200]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("AUTOBUILD_ENABLED", "AUTOBUILD_MAX_ATTEMPTS", "AI_MODE"):
        monkeypatch.delenv(key, raising=False)
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


def _run(db, code="elasticsearch", *, packages=(), image=None, shipped=None):
    """Drive the real ladder. `packages`/`image` are what the world offers."""
    proved, published, certified = [], [], []

    def run_proof(session, manifest):
        proved.append((manifest or {}).get("resource_kind", "<synthetic>"))
        return ProofOutcome(f"PROOF-{len(proved)}", "passed", "built and destroyed")

    result = autobuild.ensure(
        code, db, target="oci",
        shipped=(shipped if shipped is not None else (lambda c: None)),
        run_proof=run_proof,
        publish=lambda files: published.append(files) or list(files),
        certify=lambda m, ref: certified.append((m or {}).get("resource_kind")),
        withdraw=lambda files: list(files),
        search=lambda c, family: list(packages),
        find_image=lambda c: image,
        observe_ports=lambda ref, c: [9200])
    return result, proved, published, certified


# --- the defect -----------------------------------------------------------------

def test_software_with_no_way_to_install_it_is_refused(db):
    """THE test. Elasticsearch: nothing packaged, no repo, no archive, no image."""
    result, proved, published, certified = _run(db)

    assert result.status == "refused", result.detail
    assert proved == [], f"a proof was run for something with no recipe: {proved}"
    assert certified == [], "it was certified"
    assert published == [], "a recipe was written for something that cannot install"


def test_no_machine_is_spent_finding_that_out(db):
    """The whole point of resolving before building. This cost 120 seconds of
    cloud time and produced a false certificate."""
    _, proved, _, _ = _run(db)

    assert proved == []


def test_the_refusal_explains_and_guides(db):
    """A refusal must explain and guide, never just say no — the reviewer's
    standing rule. Somebody reading this has to know what would fix it."""
    result, _, _, _ = _run(db)

    for expected in ("repositories", "container image", "registry"):
        assert expected in result.detail, result.detail
    assert "no machine was spent" in result.detail


def test_the_refusal_is_on_the_record(db):
    result, _, _, _ = _run(db)

    stages = [a.stage for a in result.attempts]
    assert "unresolvable" in stages, stages


# --- and everything that DOES have a way in still works -------------------------

def test_a_technology_with_an_image_still_reaches_its_ladder(db):
    """The refusal must not swallow the container rung. RabbitMQ, MongoDB and
    SQL Server all arrive here with nothing packaged and an image published."""
    def shipped(code):
        return dict(SERVICE_VM) if published_marker else None

    published_marker: list = []

    def run_proof(session, manifest):
        proved.append((manifest or {}).get("resource_kind"))
        return ProofOutcome(f"P{len(proved)}", "passed", "built and destroyed")

    proved: list = []
    result = autobuild.ensure(
        "thing", db, target="oci",
        shipped=lambda c: dict(SERVICE_VM) if published_marker else None,
        run_proof=run_proof,
        publish=lambda files: published_marker.append(files) or list(files),
        certify=lambda m, ref: None, withdraw=lambda files: list(files),
        search=lambda c, family: [], find_image=lambda c: IMAGE,
        observe_ports=lambda ref, c: [9200])

    assert result.status == "published", result.detail
    assert proved, "the container rung never got a machine"


def test_a_packaged_technology_still_reaches_its_ladder(db):
    """Nothing about the refusal may touch software the repositories do carry."""
    published_marker: list = []
    proved: list = []

    def run_proof(session, manifest):
        proved.append((manifest or {}).get("resource_kind"))
        return ProofOutcome(f"P{len(proved)}", "passed", "built and destroyed")

    result = autobuild.ensure(
        "thing", db, target="oci",
        shipped=lambda c: dict(SERVICE_VM) if published_marker else None,
        run_proof=run_proof,
        publish=lambda files: published_marker.append(files) or list(files),
        certify=lambda m, ref: None, withdraw=lambda files: list(files),
        search=lambda c, family: ["thing"], find_image=lambda c: None,
        observe_ports=lambda ref, c: [])

    assert result.status == "published", result.detail
    assert proved, "the package rung never got a machine"


def test_an_existing_recipe_is_still_proved_and_certified(db):
    """THE REGRESSION GUARD the reviewer asked for. Everything certified so far —
    dotnet8, keycloak, vault, rabbitmq, mssql, apache, java21 — was certified
    through a path that proves against the REAL manifest the orchestrator
    returns. That path must be untouched."""
    proved: list = []
    certified: list = []

    def run_proof(session, manifest):
        proved.append((manifest or {}).get("resource_kind"))
        return ProofOutcome("PROOF-EXISTING", "passed", "built and destroyed")

    result = autobuild.ensure(
        "nginx", db, target="oci",
        shipped=lambda c: dict(SERVICE_VM),
        run_proof=run_proof, publish=lambda files: list(files),
        certify=lambda m, ref: certified.append((m or {}).get("resource_kind")),
        withdraw=lambda files: list(files))

    assert result.status == "published", result.detail
    assert proved == ["oci-service-vm"], (
        f"the proof no longer verifies the real manifest: {proved}")
    assert certified == ["oci-service-vm"], certified


def test_the_proof_and_the_certificate_name_the_same_resource_kind(db):
    """The heart of it. A proof that verifies one kind and certifies another is
    not evidence — it is two unrelated statements filed together."""
    proved: list = []
    certified: list = []

    def run_proof(session, manifest):
        proved.append((manifest or {}).get("resource_kind"))
        return ProofOutcome("PROOF-SAME", "passed", "built and destroyed")

    autobuild.ensure(
        "nginx", db, target="oci", shipped=lambda c: dict(SERVICE_VM),
        run_proof=run_proof, publish=lambda files: list(files),
        certify=lambda m, ref: certified.append((m or {}).get("resource_kind")),
        withdraw=lambda files: list(files))

    assert proved == certified, (
        f"proved {proved} and certified {certified}")
