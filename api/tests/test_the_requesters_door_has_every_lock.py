"""Two of three gates guarded the admin's door and not the requester's.

`build` is what the admin endpoint and the certification runner call. It had all
three gates: must publish, must claim something checkable, installed-is-not-
running. `_ensure_vm_service` is what a REAL REQUEST goes through -- `ensure`'s
ladder calls it once per install method -- and it had one of the three.

So PROOF-MYSQL-20260903T164538 refused the command-line client on the admin
door, and a requester approving a MySQL request at the same moment would have
had it certified: package `mysql`, `services: []`, no port, `mysql --version`
answering, every check green, a machine with no database on it.

Two doors into the same room, and the one people use had the weaker lock. The
day's whole pattern, one more time.

WHERE THE GATES SIT IS THE POINT. A container's first pass publishes no ports
and no version BY DESIGN -- it is a question to the machine, marked
`may_certify=False` -- and gating it would break the narrowing that discovers
ports. So the gates run at the moment a recipe is about to be KEPT, and a
refusal calls `take_it_back`: the recipe was published before its proof
(publish -> prove -> withdraw), so it is withdrawn with the claim resting on it,
and the result is "failed" -- which is exactly what makes the ladder climb to
the next method instead of stopping.
"""

from __future__ import annotations

import json
import pathlib

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
    "resource_kind": "oci-service-vm", "builds": ["mysql"], "version": "1.0.0",
}

#: What the first MySQL proof actually published: the client, not the server.
CLIENT_RECIPE = {
    "code": "mysql", "builds_on": "oci/service-vm", "ports": [],
    "version_command": "mysql --version 2>&1",
    "rhel": {"packages": ["mysql"], "services": []},
}

#: A runtime. Serves nothing, correctly; its version is what a machine checks.
RUNTIME_RECIPE = {
    "code": "dotnet8", "builds_on": "oci/service-vm", "ports": [],
    "version_command": "dotnet8 --version 2>&1",
    "rhel": {"packages": ["dotnet-sdk-8.0"], "services": []},
}

#: The same technology, actually running a database.
SERVER_RECIPE = {
    "code": "mysql", "builds_on": "oci/service-vm", "ports": [3306],
    "rhel": {"packages": ["mysql-server"], "services": ["mysqld"]},
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


class Draft:
    """The shape `_ensure_vm_service` reads: files, and nothing else it needs."""

    def __init__(self, profile: dict):
        self.candidate = profile["code"]
        self.kind = "vm-service"
        self.files = {f"generated/profiles/{profile['code']}.json":
                      json.dumps(profile)}
        self.findings = []
        self.reasoning = ""


def _door(db, profile, *, image_declares, may_certify=True, proof="passed"):
    """Walk the requester's door once, with a scripted machine, and record what
    it certified and what it withdrew."""
    certified, withdrawn = [], []
    result = autobuild._ensure_vm_service(
        profile["code"], db, Draft(profile), target="oci",
        shipped=lambda c: dict(SERVICE_VM),
        run_proof=lambda s, m: ProofOutcome(
            f"PROOF-{profile['code'].upper()}-1", proof,
            "Built, verified, and torn down" if proof == "passed" else "NOT INSTALLED"),
        publish=lambda files: list(files),
        certify=lambda m, ref: certified.append(ref),
        withdraw=lambda files: withdrawn.append(files) or list(files),
        may_certify=may_certify,
        image_declares=image_declares)
    return result, certified, withdrawn


# --- the defect ------------------------------------------------------------------

def test_a_client_is_withdrawn_at_the_requesters_door_not_certified(db):
    """THE DEFECT. Proof passes -- the client installed and answered its version
    -- and the recipe was about to be certified as a database."""
    result, certified, withdrawn = _door(db, CLIENT_RECIPE,
                                         image_declares=[3306, 33060])

    assert certified == [], "the command-line client was certified as MySQL"
    assert withdrawn, "the published client recipe was left in the store"
    assert result.status == "failed", (
        "must be 'failed' so the ladder climbs to the next method")
    assert "installed files" in result.detail or "runs nothing" in result.detail


def test_a_recipe_claiming_nothing_is_withdrawn_at_the_requesters_door(db):
    """The other missing lock, on the same door."""
    nothing = {"code": "thing", "builds_on": "oci/service-vm", "ports": [],
               "rhel": {"packages": ["thing"], "services": []}}
    result, certified, withdrawn = _door(db, nothing, image_declares=None)

    assert certified == []
    assert withdrawn
    assert result.status == "failed"
    assert "claims nothing" in result.detail


# --- what must keep working ------------------------------------------------------

def test_a_runtime_is_still_certified(db):
    """`dotnet8` serves nothing and is correct. Its vendor's image declares
    nothing, so there is nothing to contradict, and its version is a claim."""
    result, certified, withdrawn = _door(db, RUNTIME_RECIPE, image_declares=[])

    assert certified == ["PROOF-DOTNET8-1"], result.detail
    assert withdrawn == []
    assert result.status == "published"


def test_the_actual_database_is_certified(db):
    result, certified, _ = _door(db, SERVER_RECIPE, image_declares=[3306, 33060])

    assert certified == ["PROOF-MYSQL-1"], result.detail
    assert result.status == "published"


def test_a_containers_first_pass_is_a_question_not_a_candidate(db):
    """THE PLACEMENT. The first pass publishes no ports and no version on
    purpose and asks the machine what it bound. `may_certify=False` marks it.
    Gating it would refuse every container technology at its first step and
    break the narrowing that discovers ports -- the fix would be worse than the
    defect."""
    first_pass = {"code": "mysql", "builds_on": "oci/service-vm", "ports": [],
                  "container": {"image": "docker.io/library/mysql", "tag": "9",
                                "digest": "sha256:" + "f" * 64,
                                "data_dir": "/var/lib/mysql",
                                "data_mount": "/var/lib/mysql"},
                  "rhel": {"packages": [], "services": ["mysql"]}}
    result, certified, withdrawn = _door(db, first_pass,
                                         image_declares=[3306, 33060],
                                         may_certify=False)

    assert result.status == "published", result.detail
    assert withdrawn == [], "the first pass was withdrawn as if it were a verdict"
    assert certified == [], "a first pass is never certified -- by design, not by gate"


def test_a_failed_proof_still_fails_first(db):
    """The gates run only on a PASSED proof. A machine that said no is a
    different fact with a different message, and it must not be relabelled."""
    result, certified, withdrawn = _door(db, CLIENT_RECIPE,
                                         image_declares=[3306], proof="failed")

    assert certified == []
    assert result.status == "failed"
    assert "did not survive a proof" in result.detail


# --- the wire ----------------------------------------------------------------------

def test_the_ladder_hands_the_vendors_declaration_to_the_requesters_door():
    """The wire, on the call that matters.

    An earlier wiring test checked `built = build(...)` at the end of `ensure`.
    That call is real and passes the evidence -- and a vm-service candidate
    returns before ever reaching it, so for every package and container
    technology it carried nothing. This is the call those actually go through.
    """
    source = pathlib.Path("api/autobuild.py").read_text(encoding="utf-8")
    at = source.index("last = _ensure_vm_service(candidate, session, attempt")
    call = source[at:source.index("\n\n", at)]

    assert "image_declares=" in call, (
        "ensure does not hand the vendor's declaration to the requester's door")
    assert "consulted" in call, (
        "what is passed is not the declaration gathered while resolving the ladder")


def test_every_recipe_in_the_store_would_still_be_certified_here():
    """SURVEYED BEFORE ENABLING, on THIS door. Same gates, same store, judged
    against what each publisher's image declares."""
    store = pathlib.Path("generated/profiles")
    if not store.is_dir():
        pytest.skip("no generated store in this checkout")
    declares = {"dotnet8": [], "keycloak": [8080, 8443, 9000], "mongodb": [27017],
                "mssql": [1433], "rabbitmq": [5672], "vault": [8200],
                "opensearch": [9200]}
    condemned = []
    for f in sorted(store.glob("*.json")):
        if f.stem not in declares:
            continue
        files = {str(f): f.read_text(encoding="utf-8")}
        if (autobuild._installed_but_not_running(files, declares[f.stem])
                or autobuild._claims_nothing(files)):
            condemned.append(f.stem)

    assert not condemned, f"would condemn recipes already certified: {condemned}"
