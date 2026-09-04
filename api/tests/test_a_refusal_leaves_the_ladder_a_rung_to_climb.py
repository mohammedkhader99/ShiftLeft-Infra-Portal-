"""The gate refused the client, and the ladder had nowhere to go.

PROOF-MYSQL-20260904T005319-DBF8CE, on the requester's own door, with the
previous commit's fix deployed and verified live. The package rung built the
command-line client; its proof passed; the door asked the registry, the vendor
said 3306 and 33060, and the client was refused, remembered, withdrawn and its
blueprint suspended -- every one of those on a real machine, through the real
path. The verdict ended: "the next install method is tried."

Nothing was tried. The run ended `failed` at 05:12:24 with no container rung
and no second proof.

The refusal branch in `ensure` climbs on what the machine DISCOVERED: a finding
names the `discovered` rung, and `{}` -- "looked, found nothing" -- reaches for
the vendor's image. But the discovery section is written only for packages
that are MISSING (configure.py: "a working install stays quiet and costs
nothing"). The client installed fine, so the report carried no such section,
`discover()` honestly answered "never asked" (None), and None is the one answer
that branch does nothing with. The evidence for the next rung was in the gate's
own hand -- the image it had just fetched, with its declared ports -- and was
dropped after the verdict.

THE TEST THAT COVERED THIS CASE modelled the machine as saying "looked, found
none" (`{}`), which a machine whose package installed never says. So it passed.

THESE TESTS DRIVE THE REQUEST PATH with `discover` answering what it really
answered. They went RED on the code that stranded the ladder.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import autobuild
from api.proof import ProofOutcome
from db.models import GoldenImage
from db.seed import seed
from db.session import Base

SERVICE_VM = {
    "ref": "oci/service-vm", "target": "oci",
    "resource_kind": "oci-service-vm", "builds": ["mysql"], "version": "1.0.0",
}

#: Measured from docker.io/library/mysql on 2026-09-03.
MYSQL_IMAGE = {
    "image": "docker.io/library/mysql", "tag": "latest",
    "digest": "sha256:" + "a" * 64, "ports": [3306, 33060],
}


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


def _recipe_of(files: dict) -> dict:
    body = next(v for k, v in files.items() if k.endswith(".json"))
    return json.loads(body)


def _request_path(db, *, discover, observe=(3306,), machine_refutes=()):
    """The real ladder, as a requester's approval drives it.

    The OS repositories carry `mysql` -- the client -- so the package rung
    wins, exactly as on the machine. Every proof passes unless the recipe
    installs a package named in `machine_refutes`: the client installs cleanly
    and so does the vendor's container. `discover` is what the machine's
    report answers when the ladder asks what it found.
    """
    published, certified, withdrawn, asked = [], [], [], []

    def run_proof(session, manifest):
        recipe = _recipe_of(published[-1])
        if set(recipe.get("rhel", {}).get("packages") or []) & set(machine_refutes):
            return ProofOutcome(f"PROOF-MYSQL-{len(published)}", "failed",
                                "Built, but did not verify healthy: "
                                f"{recipe['rhel']['packages'][0]} NOT INSTALLED")
        return ProofOutcome(f"PROOF-MYSQL-{len(published)}", "passed",
                            "Built, verified, and torn down")

    def find_image(code):
        asked.append(code)
        return dict(MYSQL_IMAGE)

    result = autobuild.ensure(
        "mysql", db, target="oci",
        shipped=lambda c: dict(SERVICE_VM) if published else None,
        run_proof=run_proof,
        publish=lambda files: published.append(files) or list(files),
        certify=lambda m, ref: certified.append(ref),
        withdraw=lambda files: withdrawn.append(files) or list(files),
        search=lambda c, family: ["mysql"],
        find_image=find_image,
        discover=discover,
        observe_ports=lambda ref, c: list(observe))
    return result, published, certified, asked


def test_a_refused_package_reaches_the_vendors_image_when_the_machine_had_nothing_to_discover(db):
    """THE DEFECT. The machine was never asked what else it could install --
    its package installed, so the report has no discovery section -- and the
    ladder stopped on that silence, holding the vendor's image the whole time."""
    result, published, certified, _ = _request_path(
        db, discover=lambda ref, code: None)

    assert certified, (
        f"the ladder stopped after refusing the client: {result.status}: "
        f"{result.detail}")
    last = _recipe_of(published[-1])
    assert "container" in last, f"what was certified is not the vendor's image: {last}"
    assert last["container"]["image"] == "docker.io/library/mysql"
    assert last["ports"] == [3306], "the narrowed ports did not reach the recipe"


def test_the_registry_is_asked_exactly_once_on_that_path(db):
    """The door fetched the declaration to judge the client. That fetch IS the
    evidence for the container rung; paying for it again would be the same
    twice-bought answer this ladder exists to avoid."""
    _, _, certified, asked = _request_path(db, discover=lambda ref, code: None)

    assert certified
    assert asked == ["mysql"], (
        f"the registry was asked {len(asked)} times; the gate's one call was "
        f"already the answer the container rung needed")


def _kinds(published) -> list[str]:
    """What each published recipe was, in order: a package name or 'container'."""
    return ["container" if "container" in r else r["rhel"]["packages"][0]
            for r in (_recipe_of(f) for f in published)]


FINDING = {"package": "mysql-server", "repo_id": "ol9_appstream", "ports": []}


def test_a_machine_that_did_discover_something_is_still_heard_first(db):
    """The cheaper rung keeps its place. A finding names a package and the
    unit it provides (`profile_from` starts the package's own unit); that is
    tried before any container, and when its machine passes, no container is
    built at all."""
    result, published, certified, _ = _request_path(
        db, discover=lambda ref, code: dict(FINDING))

    assert certified, result.detail
    assert _kinds(published) == ["mysql", "mysql-server"], _kinds(published)


def test_the_container_follows_a_discovered_rung_the_machine_refutes(db):
    """And when the machine says the discovered package does not run either,
    the vendor's image is still there to climb to -- after it, not instead."""
    result, published, certified, _ = _request_path(
        db, discover=lambda ref, code: dict(FINDING), machine_refutes=("mysql-server",))

    assert certified, result.detail
    kinds = _kinds(published)
    assert kinds[:3] == ["mysql", "mysql-server", "container"], kinds
    assert _recipe_of(published[-1])["container"]["image"] == "docker.io/library/mysql"


# --- the door keeps what it fetched, and takes the golden image with it ---------

class _Draft:
    def __init__(self, profile):
        self.candidate = profile["code"]
        self.kind = "vm-service"
        self.files = {"generated/profiles/%s.json" % profile["code"]: json.dumps(profile)}
        self.findings = []
        self.reasoning = ""


SILENT = {"code": "mysql", "builds_on": "oci/service-vm", "ports": [],
          "version_command": "mysql --version 2>&1",
          "rhel": {"packages": ["mysql"], "services": []}}


def _door(db, profile, *, image_declares=None):
    certified = []
    result = autobuild._ensure_vm_service(
        profile["code"], db, _Draft(profile), target="oci",
        shipped=lambda c: dict(SERVICE_VM),
        run_proof=lambda s, m: ProofOutcome("PROOF-X-1", "passed", "Built"),
        publish=lambda files: list(files),
        certify=lambda m, ref: certified.append(ref),
        withdraw=lambda files: list(files),
        may_certify=True, image_declares=image_declares,
        find_image=lambda code: dict(MYSQL_IMAGE))
    return result, certified


def test_the_door_keeps_the_image_it_fetched(db):
    """Not only the ports. The ladder needs the image reference and digest to
    draft the container rung, and the door had them in hand when it refused."""
    result, certified = _door(db, SILENT)

    assert certified == []
    assert result.image.get("image") == "docker.io/library/mysql", (
        "the door judged on the vendor's image and then dropped it")
    assert result.image.get("digest") == MYSQL_IMAGE["digest"]


def test_the_door_retires_the_golden_image_of_the_proof_it_refuses(db):
    """The proof's machine was captured as a golden image BEFORE the gate
    judged the recipe -- PROOF-MYSQL-20260904T005319-DBF8CE left an available
    image of the command-line client in the tenancy. `usable_for` offers the
    newest available image for a code, so a later certified MySQL whose own
    capture failed (four of five MySQL captures have) would boot the client."""
    db.add(GoldenImage(technology_code="mysql", deployment_target="oci",
                       image_ocid="ocid1.image.oc1.me-dubai-1." + "a" * 60,
                       proof_reference="PROOF-X-1", state="available"))
    db.commit()

    result, certified = _door(db, SILENT)

    assert certified == []
    row = db.scalars(select(GoldenImage).where(
        GoldenImage.proof_reference == "PROOF-X-1")).one()
    assert row.state == "withdrawn", (
        f"the image of a refused recipe is still {row.state}, and the fast path "
        f"would boot it")
    assert "runs nothing" in (row.detail or "") or "starts no service" in (row.detail or "")
