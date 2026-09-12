"""Proof builds: the runner's authority, and its limits (C2).

ARCHITECTURE.md §4 gives the certification runner its own authority to provision
without a Jira approval. That is a deliberate widening of a hard rule, granted on
2026-08-21, and it is only safe because of the bounds around it. These tests are
those bounds.

The teardown scoping tests matter most. The sandbox is `Development` — a real
tier holding real environments people are using — so "everything the runner
built" and "everything in Development" must never become the same query. A bug
there does not fail a proof; it deletes somebody's work.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import proof
from db.models import Blueprint, CertificationProof
from db.seed import seed
from db.session import Base

_PROOF_ENV = ("CERTIFICATION_PROOF_ENABLED", "CERTIFICATION_SANDBOX_TIER",
              "CERTIFICATION_COST_CAP_MONTHLY", "CERTIFICATION_VALIDITY_DAYS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _PROOF_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.add(Blueprint(technology_code="oci-oke", deployment_target="oci",
                    blueprint_ref="oci/oke", status="certified",
                    resource_kind="oci-oke"))
    s.commit()
    yield s
    s.close()


def allow(monkeypatch, tier="Development", cap="250"):
    monkeypatch.setenv("CERTIFICATION_PROOF_ENABLED", "true")
    monkeypatch.setenv("CERTIFICATION_SANDBOX_TIER", tier)
    monkeypatch.setenv("CERTIFICATION_COST_CAP_MONTHLY", cap)


class Recorder:
    """Captures every handoff so a test can assert what was and was NOT sent."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, dict]] = []
        self.results = results or {}

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if path in self.results:
            return self.results[path]
        if path == "/provision":
            # Answer the way the real orchestrator does: a plan summary naming
            # the module it came from. run_proof reads this to check the proof is
            # building the kind under test — a fake that just says "ok" would let
            # a proof of the wrong thing pass, which is the defect of 2026-08-22.
            kind = payload.get("resource_kind") or ""
            return True, '{"plan_summary": "%s: Plan: 1 to add, 0 to change."}' % kind
        return True, "ok"

    def paths(self):
        return [p for p, _ in self.calls]

    def references(self):
        return [pl.get("reference") for _, pl in self.calls]


def bp(db):
    return db.get(Blueprint, ("oci-oke", "oci"))


# --- Refusals: it never begins ----------------------------------------------

def test_proof_builds_are_off_by_default(db, monkeypatch):
    """A proof creates real, billable infrastructure. Nothing spends by accident."""
    post = Recorder()
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                          verify=lambda r, p: (True, "ok"))
    assert out.status == "refused"
    assert "CERTIFICATION_PROOF_ENABLED" in out.detail
    assert post.calls == [], "something was sent despite proofs being disabled"


def test_an_unset_sandbox_tier_refuses_rather_than_choosing_one(db, monkeypatch):
    """ARCHITECTURE.md §4: it refuses to start rather than default to somewhere.
    Defaulting is how a proof ends up in Production."""
    monkeypatch.setenv("CERTIFICATION_PROOF_ENABLED", "true")
    post = Recorder()
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                          verify=lambda r, p: (True, "ok"))
    assert out.status == "refused"
    assert "CERTIFICATION_SANDBOX_TIER" in out.detail
    assert post.calls == []


def test_a_tier_that_is_not_a_tier_refuses_and_says_so(db, monkeypatch):
    """A typo would otherwise become a tier no request matches, and the runner
    would look idle rather than misconfigured."""
    allow(monkeypatch, tier="Devlopment")          # deliberate typo
    post = Recorder()
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                          verify=lambda r, p: (True, "ok"))
    assert out.status == "refused"
    assert "Devlopment" in out.detail and "Development" in out.detail
    assert post.calls == []


# --- The cost ceiling, checked BEFORE anything is built ----------------------

def test_a_plan_over_the_cap_is_refused_before_apply(db, monkeypatch):
    """It never discovers a price by paying it."""
    allow(monkeypatch, cap="250")
    post = Recorder()
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 1240.0,
                          verify=lambda r, p: (True, "ok"))

    assert out.status == "refused"
    assert "1,240.00" in out.detail and "250.00" in out.detail
    assert post.calls == [], "it contacted the orchestrator despite being over cap"


def test_an_unpriceable_plan_is_refused_not_waved_through(db, monkeypatch):
    """'We could not work out what this costs' is not a reason to spend money."""
    allow(monkeypatch)
    post = Recorder()
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: None,
                          verify=lambda r, p: (True, "ok"))
    assert out.status == "refused"
    assert post.calls == []


# --- Teardown scoping: THE test that matters --------------------------------

def test_the_runner_may_not_destroy_anything_it_did_not_create():
    """Scoped by MARKER, never by tier.

    A tier-scoped runner would return True for every one of these, because they
    all live in the sandbox tier it is entitled to build in.
    """
    assert proof.may_destroy("PROOF-OCI-OKE-20260821T093000") is True
    # Somebody's real environment, in the same tier.
    assert proof.may_destroy("REQ-2026-0153") is False
    # A user environment whose name merely starts like a proof.
    assert proof.may_destroy("PROOF-of-concept") is False
    assert proof.may_destroy("proof-oci-oke-20260821T093000") is False
    assert proof.may_destroy("") is False
    assert proof.may_destroy(None) is False


def test_a_proof_only_ever_tears_down_its_own_reference(db, monkeypatch):
    """Whatever else it does, every destroy it sends names its own proof."""
    allow(monkeypatch)
    post = Recorder()
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                          verify=lambda r, p: (True, "ok"))

    destroys = [pl for p, pl in post.calls if p == "/destroy"]
    assert destroys, "it never tore down what it built"
    for payload in destroys:
        assert payload["reference"] == out.reference
        assert proof.may_destroy(payload["reference"]), payload["reference"]
        assert payload["proof"] is True, "the teardown did not declare itself a proof"


def test_every_handoff_is_confined_to_the_sandbox_tier(db, monkeypatch):
    allow(monkeypatch, tier="Development")
    post = Recorder()
    proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                    verify=lambda r, p: (True, "ok"))
    assert post.calls
    for _path, payload in post.calls:
        assert payload["policy_input"]["environment_tier"] == "Development"


# --- Outcomes ----------------------------------------------------------------

def test_a_working_blueprint_passes_and_is_torn_down(db, monkeypatch):
    allow(monkeypatch)
    post = Recorder()
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                          verify=lambda r, p: (True, "healthy"))

    assert out.status == "passed", out.detail
    assert post.paths() == ["/provision", "/apply", "/destroy"]
    row = db.scalars(select(CertificationProof)).one()
    assert row.status == "passed" and row.finished_at is not None
    assert float(row.planned_monthly) == 10.0


def test_a_broken_blueprint_fails_and_is_still_torn_down(db, monkeypatch):
    """A failed apply may still have created something before it failed."""
    allow(monkeypatch)
    post = Recorder({"/apply": (False, "Invalid Kubernetes version v1.29.1")})
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                          verify=lambda r, p: (True, "ok"))

    assert out.status == "failed"
    assert "v1.29.1" in out.detail
    assert "/destroy" in post.paths(), "a failed apply was not cleaned up"


def test_built_but_unhealthy_is_a_failure(db, monkeypatch):
    """Terraform exiting zero says an API call was accepted. REQ-2026-0150 built
    a cluster whose nodes never registered — apply succeeded, the thing did not
    work."""
    allow(monkeypatch)
    post = Recorder()
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                          verify=lambda r, p: (False, "node pool never reached ACTIVE"))

    assert out.status == "failed"
    assert "did not verify healthy" in out.detail
    assert "/destroy" in post.paths()


def test_a_proof_that_cannot_tear_down_is_abandoned_and_says_so(db, monkeypatch):
    """The loudest outcome there is: the runner left something billing.

    Reported as its own status rather than folded into 'failed', because the
    action required is completely different — somebody has to go and look.
    """
    allow(monkeypatch)
    post = Recorder({"/destroy": (False, "state lock held")})
    out = proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                          verify=lambda r, p: (True, "healthy"))

    assert out.status == "abandoned"
    assert "may still be running" in out.detail
    assert out.reference in out.detail, "it did not say WHAT to go and look for"


def test_the_attempt_is_recorded_before_the_build_starts(db, monkeypatch):
    """A proof that dies mid-flight must leave a row saying so, not nothing."""
    allow(monkeypatch)

    seen = {}

    def post(path, payload):
        if path == "/provision":
            rows = db.scalars(select(CertificationProof)).all()
            seen["during"] = [(r.reference, r.status) for r in rows]
        return True, "ok"

    proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                    verify=lambda r, p: (True, "ok"))
    assert seen["during"], "no record existed while the build was running"
    assert seen["during"][0][1] == "running"


# --- Expiry ------------------------------------------------------------------

def test_certification_does_not_expire_while_proofs_are_switched_off(db, monkeypatch):
    """The hazard in coupling a clock to a feature nobody enabled.

    With proof builds off there is nothing to keep the clock fresh, so applying
    the 30-day rule would take the whole catalogue offline on day 31 — worse than
    the staleness it was meant to prevent.
    """
    from api import certification

    assert certification.expire(db) == []
    assert bp(db).status == "certified"


def test_never_proven_is_stale_ONCE_THE_FEATURE_HAS_BEEN_RUNNING(db, monkeypatch):
    """Never proven by a build is stale, and says so plainly — but only after
    proofs have been running long enough for one to have happened.

    This test used to assert that merely ENABLING proofs expired an unproven
    blueprint. It encoded the defect: on 2026-08-21 that took all seven
    hand-certified blueprints offline the moment the feature was switched on, and
    every request went to manual fulfilment.
    """
    from datetime import datetime, timedelta, timezone

    from api import certification

    allow(monkeypatch)
    long_ago = datetime.now(timezone.utc) - timedelta(days=45)
    db.add(CertificationProof(
        technology_code="something-else", deployment_target="oci",
        resource_kind="oci-other", reference="PROOF-OTHER-20260707T090000",
        status="passed", started_at=long_ago, finished_at=long_ago))
    db.commit()

    expired = certification.expire(db)
    assert len(expired) == 1, expired
    row = bp(db)
    assert row.status == certification.STALE
    assert "never been proven" in row.notes


def test_a_recent_passing_proof_keeps_it_certified(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from api import certification

    allow(monkeypatch)
    db.add(CertificationProof(
        technology_code="oci-oke", deployment_target="oci", resource_kind="oci-oke",
        reference="PROOF-OCI-OKE-20260820T090000", status="passed",
        finished_at=datetime.now(timezone.utc) - timedelta(days=3)))
    db.commit()

    assert certification.expire(db) == []
    assert bp(db).status == "certified"


def test_staleness_is_measured_from_the_last_PASSING_proof(db, monkeypatch):
    """A recent failure is not freshness. Counting any proof would let a
    blueprint that fails nightly look continuously proven."""
    from datetime import datetime, timedelta, timezone

    from api import certification

    allow(monkeypatch)
    # Failing since proofs began 45 days ago: the grace window has long passed,
    # so the only question left is whether a FAILURE counts as freshness.
    began = datetime.now(timezone.utc) - timedelta(days=45)
    db.add(CertificationProof(
        technology_code="oci-oke", deployment_target="oci", resource_kind="oci-oke",
        reference="PROOF-OCI-OKE-20260707T090000", status="failed",
        started_at=began, finished_at=datetime.now(timezone.utc)))
    db.commit()

    expired = certification.expire(db)
    assert len(expired) == 1, "a failing proof was treated as freshness"
    assert bp(db).status == certification.STALE


# --- The way back ------------------------------------------------------------

def _passing_proof(db, days_ago=1, tech="oci-oke"):
    from datetime import datetime, timedelta, timezone
    db.add(CertificationProof(
        technology_code=tech, deployment_target="oci", resource_kind=tech,
        reference=f"PROOF-{tech.upper()}-2026082{days_ago}T090000", status="passed",
        finished_at=datetime.now(timezone.utc) - timedelta(days=days_ago)))
    db.commit()


def test_a_passing_proof_brings_a_suspended_blueprint_back(db, monkeypatch):
    """What C1 promised: coming back is earned by evidence, not by someone
    deciding it is probably fine now."""
    from api import certification

    allow(monkeypatch)
    row = bp(db)
    row.status = certification.SUSPENDED
    row.certified_by = "mohammed.khader@emaratechg.ae"
    db.commit()
    _passing_proof(db)

    back = certification.restore(db, builds={"oci-oke"})
    db.commit()
    assert len(back) == 1 and back[0]["was"] == certification.SUSPENDED
    assert bp(db).status == "certified"
    assert "Re-certified by a passing proof" in bp(db).notes


def test_a_passing_proof_certifies_even_without_a_human_fingerprint(db, monkeypatch):
    """CHANGED 2026-08-21 by the reviewer's decision, recorded in ARCHITECTURE §7.

    This asserted the opposite: that first certification stayed a human act,
    because a passing build says a recipe WORKS and not that it is SAFE. The
    reviewer raised the requirement three times and it is mandatory — the human
    is out of the certification path, and the strict IaC scan answers the safety
    question instead (§8).

    A proof is still required. Nothing is certified without one.

    `builds` is supplied because since 2026-08-24 a restoration also needs the
    RECIPE to still exist — see the test below. That is a machine-checkable
    fact, not a human in the path, so the requirement this test exists for is
    untouched.
    """
    from api import certification

    allow(monkeypatch)
    row = bp(db)
    row.status = certification.SUSPENDED
    row.certified_by = None          # nobody ever approved this
    db.commit()
    _passing_proof(db)

    restored = certification.restore(db, builds={"oci-oke"})
    db.commit()
    assert len(restored) == 1, restored
    assert bp(db).status == "certified"


def test_a_passing_proof_does_not_restore_a_recipe_that_is_GONE(db, monkeypatch):
    """A PASSING PROOF IS NOT A RECIPE, and this cost a real machine.

    REQ-2026-0193's container proof passed and certified; the narrowing proof
    that followed failed and the profile was withdrawn from the generated store.
    `take_it_back` suspended the certification — and this poller, looking only
    at proofs, re-certified it within one cycle. The catalogue went back to
    claiming RabbitMQ with nothing to build it, and the next request would have
    got another bare machine reported as provisioned.
    """
    from api import certification

    allow(monkeypatch)
    row = bp(db)
    row.status = certification.SUSPENDED
    db.commit()
    _passing_proof(db)

    # The orchestrator no longer builds it: the recipe was withdrawn.
    assert certification.restore(db, builds=frozenset()) == []
    assert bp(db).status == certification.SUSPENDED


def test_a_restoration_that_cannot_ASK_does_not_guess(db, monkeypatch):
    """Absent means we could not reach the orchestrator, and the safe direction
    is not to restore: a delayed restoration costs a request its automatic path,
    while a wrong one costs a machine and tells nobody."""
    from api import certification

    allow(monkeypatch)
    row = bp(db)
    row.status = certification.SUSPENDED
    db.commit()
    _passing_proof(db)

    assert certification.restore(db, builds=None) == []
    assert bp(db).status == certification.SUSPENDED


def test_a_failed_proof_does_not_bring_anything_back(db, monkeypatch):
    from datetime import datetime, timezone

    from api import certification

    allow(monkeypatch)
    row = bp(db)
    row.status = certification.SUSPENDED
    row.certified_by = "mohammed.khader@emaratechg.ae"
    db.add(CertificationProof(
        technology_code="oci-oke", deployment_target="oci", resource_kind="oci-oke",
        reference="PROOF-OCI-OKE-20260821T100000", status="failed",
        finished_at=datetime.now(timezone.utc)))
    db.commit()

    assert certification.restore(db) == []
    assert bp(db).status == certification.SUSPENDED


def test_an_old_passing_proof_is_not_enough_to_come_back(db, monkeypatch):
    """Freshness cuts both ways: a proof that passed 60 days ago is exactly the
    evidence the validity period says has expired."""
    from api import certification

    allow(monkeypatch)
    row = bp(db)
    row.status = certification.STALE
    row.certified_by = "mohammed.khader@emaratechg.ae"
    db.commit()
    _passing_proof(db, days_ago=60)

    assert certification.restore(db) == []
    assert bp(db).status == certification.STALE


def test_evidence_against_wins_within_one_sweep(db, monkeypatch):
    """Order matters. If restoration ran first, a blueprint could be re-certified
    and then immediately suspended again in the same cycle — or worse, the other
    way round, leaving something broken on offer until the next sweep."""
    import inspect

    from api import main

    src = inspect.getsource(main._poll_once)

    # WHAT THIS GUARD IS FOR, because the failure without it is baffling.
    #
    # `inspect.getsource` reads the file from disk and slices it at the line
    # numbers the LOADED code object carries. Edit api/main.py while the suite is
    # running — inserting a fix a thousand lines above this function, say — and
    # those two disagree: the slice comes back as somebody else's function, the
    # `.index` calls below raise ValueError, and a test about certification
    # ordering fails for reasons invisible in its own source.
    #
    # That happened on 2026-09-12 and was diagnosed as a flaky test before the
    # mechanism was understood. It is not flaky. It is reading a file that moved.
    assert "def _poll_once" in src, (
        "inspect.getsource did not return _poll_once. api/main.py has almost "
        "certainly changed on disk since this process imported it — do not edit "
        "source while the suite is running. Re-run before believing this result.")

    for earlier in ("certification.review", "certification.expire"):
        assert earlier in src and "certification.restore" in src, (
            f"{earlier} or certification.restore is missing from _poll_once")
        assert src.index(earlier) < src.index("certification.restore")


def test_the_handoff_carries_every_key_the_orchestrator_requires(db, monkeypatch):
    """Found live, not in a test. A valid proof returned 500 with
    KeyError: 'idempotency_key' — /provision and /apply both index it directly,
    so a payload without it is an error rather than a refusal, and an error tells
    nobody which bound was exceeded.

    Asserted against the endpoints' ACTUAL requirements, read from the
    orchestrator source, so a new required key fails here rather than at runtime.
    """
    import ast
    import pathlib

    allow(monkeypatch)
    post = Recorder()
    proof.run_proof(db, bp(db), post=post, price=lambda c: 10,
                    verify=lambda r, p: (True, "ok"))

    src = pathlib.Path(__file__).resolve().parents[2] / "orchestrator" / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    required: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in ("provision", "apply", "verify_boot", "destroy"):
            continue
        keys = set()
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name)
                    and sub.value.id == "payload"
                    and isinstance(sub.slice, ast.Constant)):
                keys.add(sub.slice.value)
        required[node.name] = keys

    needed = set().union(*required.values()) if required else set()
    assert needed, "could not read the orchestrator's requirements"
    for _path, payload in post.calls:
        missing = needed - set(payload)
        assert not missing, f"the handoff omits {sorted(missing)}, required by {required}"


def test_switching_proofs_on_does_not_expire_the_whole_catalogue(db, monkeypatch):
    """FOUND IN PRODUCTION, 2026-08-21. Gating expiry on "proofs are enabled" was
    wrong: the moment they were switched on, all seven hand-certified blueprints
    aged out at once — punished retroactively for missing proofs that had never
    been possible. Every request then went to manual fulfilment.

    A blueprint cannot have failed to earn a proof before proofs existed.
    """
    from api import certification

    allow(monkeypatch)                      # proofs ENABLED, but none has ever run
    assert certification.expire(db) == [], (
        "enabling proofs expired blueprints that never had a chance to earn one")
    assert bp(db).status == "certified"


def test_the_clock_starts_when_proofs_start_not_at_the_epoch(db, monkeypatch):
    """Once proofs ARE running, a blueprint gets the same validity window as
    everything else to earn its first one — measured from when the feature began,
    not from whenever it happened to be certified."""
    from datetime import datetime, timedelta, timezone

    from api import certification

    allow(monkeypatch)
    # The feature started five days ago and has run for something else.
    db.add(CertificationProof(
        technology_code="something-else", deployment_target="oci",
        resource_kind="oci-other", reference="PROOF-OTHER-20260816T090000",
        status="passed",
        started_at=datetime.now(timezone.utc) - timedelta(days=5),
        finished_at=datetime.now(timezone.utc) - timedelta(days=5)))
    db.commit()

    # oci-oke still has no proof of its own, but only five days have passed.
    assert certification.expire(db) == []
    assert bp(db).status == "certified"


def test_a_blueprint_that_never_earns_a_proof_does_eventually_expire(db, monkeypatch):
    """The rule still bites — it just starts counting from the right moment.
    'Certified by hand and never exercised' is the oci-oke state this exists for."""
    from datetime import datetime, timedelta, timezone

    from api import certification

    allow(monkeypatch)
    long_ago = datetime.now(timezone.utc) - timedelta(days=60)
    db.add(CertificationProof(
        technology_code="something-else", deployment_target="oci",
        resource_kind="oci-other", reference="PROOF-OTHER-20260622T090000",
        status="passed", started_at=long_ago, finished_at=long_ago))
    db.commit()

    expired = certification.expire(db)
    assert len(expired) == 1, "a blueprint unproven for 60 days stayed certified"
    assert bp(db).status == certification.STALE


# --- a proof of nothing is not a proof ---------------------------------------

class TestARecipeThatBuildsNothing:
    """FOUND IN PRODUCTION, REQ-2026-0175, 2026-08-21.

    `keycloak` had no recipe, so the agent drafted one. Its Terraform says in its
    own opening line "builds nothing yet": a TODO_provider_resource guarded by
    `count = var.resource_kind == "oci-keycloak" ? 1 : 0`, which evaluated to
    zero. The invalid resource was therefore never instantiated.

    Every gate went green. The plan succeeded (creating nothing), apply succeeded
    (creating nothing), and verify found nothing to check and answered "nothing
    here files a report; it built and tore down". The proof PASSED — 156 seconds
    spent proving that Terraform can run.

    That is the deepest failure available to this design. The entire claim of
    certification-by-proof is "something real was built, confirmed working, and
    destroyed"; a recipe that creates nothing satisfies every step of it while
    meaning none of it.
    """

    def test_a_plan_that_adds_nothing_fails_the_proof(self, db, monkeypatch):
        allow(monkeypatch)
        posted = []

        def post(path, payload):
            posted.append(path)
            if path == "/provision":
                return True, ('{"plan_summary": "oci-oke: Plan: 0 to add, '
                              '0 to change, 0 to destroy."}')
            return True, "{}"

        out = proof.run_proof(db, bp(db), post=post,
                              price=lambda c: 10.0, verify=lambda r, p: (True, "ok"))

        assert out.status == "failed"
        assert "creates nothing" in out.detail
        assert "/apply" not in posted, "it built something it already knew was empty"

    def test_a_plan_that_adds_a_resource_proceeds(self, db, monkeypatch):
        allow(monkeypatch)

        def post(path, payload):
            if path == "/provision":
                return True, '{"plan_summary": "oci-oke: Plan: 1 to add."}'
            return True, "{}"

        out = proof.run_proof(db, bp(db), post=post,
                              price=lambda c: 10.0, verify=lambda r, p: (True, "ok"))
        assert out.status == "passed", out.detail

    def test_counts_are_summed_across_modules(self):
        assert proof.plans_to_create(
            "a: Plan: 0 to add, 0 to change. b: Plan: 2 to add, 0 to change.") == 2

    def test_an_unreadable_plan_is_not_mistaken_for_zero(self):
        """A different fault, needing a different fix. Treating 'we could not
        read the plan' as 'the plan is empty' would fail proofs for recipes that
        build perfectly well."""
        assert proof.plans_to_create("some other response entirely") is None
        assert proof.plans_to_create("") is None
