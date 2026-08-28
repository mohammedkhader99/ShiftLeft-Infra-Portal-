"""D2 step 2: a recipe that failed its proof must stop claiming it works.

`make_withdraw` has stated this invariant in its own docstring since it was
written:

    "a proof that fails has to take it back out. Left behind, an unproven
     profile makes an uninstallable technology look installable to every
     later request."

`_ensure_vm_service` honours it. `ensure` — the one branch that proves a recipe
it did not itself write — never called it, and that is the whole of REQ-2026-0217,
0218, 0222, 0223 and 0224:

    a run guessed `dnf install mssql`, wrote generated/profiles/mssql.json,
    proved it on a real machine and failed. The profile stayed. It made
    oci/service-vm advertise that it builds mssql, so the NEXT request found
    an existing recipe, proved the same guess, failed, and RETURNED — without
    ever reaching the ladder that knows about vendor repositories and
    containers.

Five requests, five machines, one stale file. The ladder had a container rung
for SQL Server the whole time and the run never got far enough to use it.

THE SHAPE IS ONE I HAD ALREADY NAMED AND FIXED TWICE, one level down, in
test_the_agent_acts_on_what_the_repository_said.py:

    "a refusal about one rung's recipe ends the whole run, so a technology is
     stranded on whichever rung happened to be refused first"

Each time I fixed the instance in front of me. This is the same sentence about
the function that CALLS the ladder rather than about a rung inside it.

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
    "ref": "oci/service-vm",
    "target": "oci",
    "resource_kind": "oci-service-vm",
    "builds": ["nginx", "redis7", "mssql"],
    "version": "1.0.0",
}

#: What `registry.find` returns for SQL Server now that the first-party
#: catalogues are searched. Measured values, frozen.
MSSQL_IMAGE = {
    "image": "mcr.microsoft.com/mssql/server",
    "tag": "latest",
    "digest": "sha256:" + "b" * 64,
    "ports": [1433],
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


class Store:
    """The generated store, as `ensure` sees it through its collaborators.

    `reviewed=True` models a recipe a PERSON wrote: it is not in the generated
    store, so `withdraw` finds nothing to delete and reports nothing — which is
    exactly how the real `make_withdraw` distinguishes the two.
    """

    def __init__(self, *, present: bool = True, reviewed: bool = False):
        self.present = present
        self.reviewed = reviewed
        self.withdrawn: list[str] = []
        self.published: list[dict] = []

    def shipped(self, candidate):
        return dict(SERVICE_VM) if self.present else None

    def withdraw(self, files):
        if self.reviewed or not self.present:
            return []
        self.present = False
        removed = [f"generated/profiles/{name}" for name in (files or {})]
        self.withdrawn.extend(removed)
        return removed

    def publish(self, files):
        self.present = True
        self.published.append(files)
        return list(files or {})


def _run(db, store, *, proofs, image=None, **kw):
    """Drive the real `ensure`, scripting what each machine would report."""
    seen: list[str] = []

    def run_proof(session, manifest):
        status = proofs[min(len(seen), len(proofs) - 1)]
        seen.append(status)
        return ProofOutcome(
            f"PROOF-MSSQL-2026082{len(seen)}", status,
            "Built, verified, and torn down" if status == "passed"
            else "mssql NOT INSTALLED")

    result = autobuild.ensure(
        "mssql", db, target="oci", shipped=store.shipped, run_proof=run_proof,
        publish=store.publish, certify=lambda m, ref: None,
        withdraw=store.withdraw, search=lambda code, family: [],
        find_image=(lambda code: image), observe_ports=lambda ref, code: [],
        **kw)
    return result, seen


# --- the recipe stops claiming it works ---------------------------------------

def test_a_recipe_that_failed_its_proof_is_withdrawn(db):
    """THE test. Without it the same guess is re-proved on a new machine every
    time anybody asks for SQL Server, for as long as anybody keeps asking."""
    store = Store()

    _run(db, store, proofs=["failed"])

    assert store.withdrawn == ["generated/profiles/mssql.json"], (
        "the failed recipe is still in the store advertising mssql as "
        "installable, so the next request will prove the same guess again")


def test_the_store_no_longer_offers_it_afterwards(db):
    """REQ-2026-0217 through 0224 in one assertion: the second request must not
    find the recipe that the first one disproved."""
    store = Store()

    _run(db, store, proofs=["failed"])

    assert store.shipped("mssql") is None, (
        "oci/service-vm still advertises that it builds mssql")


def test_a_failed_recipe_no_longer_ends_the_run(db):
    """The run continued to the ladder, and the ladder is where the container
    rung lives — the one rung SQL Server could ever have used."""
    store = Store()

    result, proved = _run(db, store, proofs=["failed", "passed"],
                          image=MSSQL_IMAGE)

    assert len(proved) > 1, (
        "the run stopped at the recipe that failed and never reached the "
        "install methods")
    assert result.status == "published", result.detail


def test_the_retirement_is_on_the_record(db):
    """Somebody reading the audit trail later must see that a recipe was
    retired, and why — not merely that a proof failed."""
    store = Store()

    result, _ = _run(db, store, proofs=["failed"])

    retired = [a for a in result.attempts if a.stage == "retired"]
    assert retired, [a.stage for a in result.attempts]
    assert "withdrawn" in retired[0].outcome or "withdrawn" in retired[0].detail
    assert "mssql" in retired[0].detail


# --- and only our own mistakes ------------------------------------------------

def test_a_recipe_a_person_reviewed_is_never_withdrawn(db):
    """`withdraw` is confined to the generated store and reports what it
    actually deleted. A reviewed recipe removes nothing, so the run ends exactly
    as it did before — we retire our own mistakes and nobody else's."""
    store = Store(reviewed=True)

    result, proved = _run(db, store, proofs=["failed"], image=MSSQL_IMAGE)

    assert store.withdrawn == []
    assert result.status == "failed"
    assert "NOT been certified" in result.detail
    assert len(proved) == 1, "it went on to write over a reviewed recipe"


def test_a_recipe_that_passes_is_left_exactly_where_it_is(db):
    """The nginx case, and it must not change: a recipe nobody had certified is
    proved and certified, and nothing is written or removed."""
    store = Store()

    result, proved = _run(db, store, proofs=["passed"])

    assert result.status == "published"
    assert store.withdrawn == []
    assert store.published == []
    assert proved == ["passed"]


def test_without_a_withdraw_collaborator_the_old_behaviour_stands(db):
    """`withdraw` is optional in the signature, and a caller that passes none
    must not silently lose the failure."""
    calls = []

    def run_proof(session, manifest):
        calls.append(manifest)
        return ProofOutcome("PROOF-MSSQL-1", "failed", "mssql NOT INSTALLED")

    result = autobuild.ensure(
        "mssql", db, target="oci", shipped=lambda c: dict(SERVICE_VM),
        run_proof=run_proof, publish=lambda f: [], certify=lambda m, r: None)

    assert result.status == "failed"
    assert len(calls) == 1


# --- and it is remembered, so no second machine proves it again ---------------
#
# Retiring a failed recipe stops it advertising itself, but the ladder may draft
# the very same recipe straight back. RabbitMQ is exactly that case: the recipe
# in its store and the only rung its ladder offers are both the same container
# image, so a failure bought two machines to learn one thing.

def _container_recipe(code, image):
    """Precisely what the container rung will draft, so the two fingerprints
    match — which is the whole condition being tested."""
    from api import ai_blueprint
    return autobuild._recipe_in(
        ai_blueprint.draft_from_image(code, image, target="oci"))


def test_the_retired_recipe_is_not_proved_a_second_time(db):
    """THE test for the double proof. One machine, not two."""
    store = Store()
    same = _container_recipe("mssql", MSSQL_IMAGE)

    _, proved = _run(db, store, proofs=["failed"], image=MSSQL_IMAGE,
                     stored=lambda code: same)

    assert len(proved) == 1, (
        f"{len(proved)} machines were built; the ladder re-proved the recipe "
        "that had just been retired")


def test_a_DIFFERENT_recipe_is_still_tried(db):
    """The memory is keyed on the recipe, not the technology. SQL Server's
    stored recipe is a package guess and its ladder offers a container — a
    genuinely different thing, and it must still get its machine."""
    store = Store()

    _, proved = _run(db, store, proofs=["failed", "passed"], image=MSSQL_IMAGE,
                     stored=lambda code: {"code": "mssql", "rhel": {
                         "packages": ["mssql"], "services": []}})

    assert len(proved) == 2, "the container rung was skipped as if already tried"


def test_the_recipe_is_read_before_it_is_deleted(db):
    """Order is the whole mechanism: after `withdraw` there is no recipe left to
    read, so a reader called afterwards remembers nothing."""
    order = []
    store = Store()
    real_withdraw = store.withdraw

    def watched_withdraw(files):
        order.append("withdraw")
        return real_withdraw(files)

    store.withdraw = watched_withdraw

    def stored(code):
        order.append("read")
        return _container_recipe("mssql", MSSQL_IMAGE)

    _run(db, store, proofs=["failed"], image=MSSQL_IMAGE, stored=stored)

    assert order[:2] == ["read", "withdraw"], order


def test_nothing_is_remembered_when_the_recipe_cannot_be_read(db):
    """`stored` is optional. A caller that passes none must behave as before —
    a wasted machine is bad; a crash is worse."""
    store = Store()

    result, proved = _run(db, store, proofs=["failed", "passed"],
                          image=MSSQL_IMAGE)

    assert result.status == "published"
    assert len(proved) == 2


def test_nothing_is_remembered_when_nothing_was_retired(db):
    """A recipe a person reviewed is not withdrawn, so it must not be recorded
    as refuted either — that memory would strand a reviewed recipe."""
    from api import recipe_memory

    store = Store(reviewed=True)
    same = _container_recipe("mssql", MSSQL_IMAGE)

    _run(db, store, proofs=["failed"], image=MSSQL_IMAGE, stored=lambda c: same)

    assert recipe_memory.previously_refuted(db, "mssql", "oci", same) == ""


# --- reading the store safely -------------------------------------------------

def test_make_stored_reads_a_published_recipe(tmp_path):
    from api import proof_wiring

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "mssql.json").write_text('{"code": "mssql"}', encoding="utf-8")

    assert proof_wiring.make_stored(tmp_path)("mssql") == {"code": "mssql"}


def test_make_stored_never_reads_outside_the_generated_store(tmp_path):
    """The same confinement `publish` and `withdraw` have. A candidate name
    reaches this function from a catalogue row, and what it returns is read as a
    recipe — so an escape here reads any file the API can reach.

    THREE INDEPENDENT MECHANISMS defend this, and the test asserts the PROPERTY
    rather than any one of them: a leading dot is refused, the name is reduced
    to its last segment, and the resolved path must stay under the store.

    The inputs below are chosen to reach DIFFERENT ones. `../secret` never gets
    past the leading-dot check, so it proves nothing about the other two —
    which is exactly the mistake this docstring exists to stop somebody
    repeating: a test that passes for a reason it did not intend is a test that
    will not notice when that reason is removed."""
    from api import proof_wiring

    (tmp_path / "profiles").mkdir()
    (tmp_path.parent / "secret.json").write_text('{"a": 1}', encoding="utf-8")

    stored = proof_wiring.make_stored(tmp_path)
    assert stored("../secret") is None           # the leading-dot check
    assert stored("/etc/passwd") is None         # an absolute path
    assert stored("x/../../../secret") is None   # normalisation + confinement


def test_make_stored_is_silent_about_what_is_not_there(tmp_path):
    from api import proof_wiring

    (tmp_path / "profiles").mkdir()
    stored = proof_wiring.make_stored(tmp_path)

    assert stored("absent") is None
    (tmp_path / "profiles" / "broken.json").write_text("not json", encoding="utf-8")
    assert stored("broken") is None
