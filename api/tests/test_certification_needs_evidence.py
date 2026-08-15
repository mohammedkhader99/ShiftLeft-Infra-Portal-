"""Certification must rest on a machine, not on somebody's recollection.

Standing requirement (user, 15 Aug 2026): "once you certify the component it
should not fail when the user selects the component."

Until now that depended entirely on whoever clicked certify remembering whether
the thing had ever been booted. Today alone, four technologies looked ready on
that basis and two of them were selling a version their OS does not deliver —
Python 3.9 as 3.12, Node 18 as 20. Certifying on "the package installs" would
have put the portal's automated stamp on both.

So the rule is now enforced rather than intended: for every OS family a blueprint
offers a technology on, a real machine must have been built and asked. A family
that was built and DISPROVEN passes the gate, because the form already refuses
those images and no requester can reach the broken combination. A family nobody
has ever tried does not.
"""

from __future__ import annotations

import pytest

from api import blueprint_capabilities as caps
from api import main as api_main


def _shipped(verified, refuted=None, families=("rhel", "debian")):
    return [{"ref": "oci/service-vm", "target": "oci", "resource_kind": "oci-service-vm",
             "builds": ["java21", "python312"], "os_families": list(families),
             "verified": verified, "refuted": refuted or {}}]


@pytest.fixture(autouse=True)
def _clean():
    caps.reset()
    yield
    caps.reset()


def _unproven(code, verified, refuted=None, families=("rhel", "debian")):
    caps.refresh(lambda: _shipped(verified, refuted, families))
    manifest = {"os_families": list(families)}
    return api_main._unproven_families(code, manifest)


# --- The rule -----------------------------------------------------------------

def test_a_technology_proven_everywhere_it_is_offered_may_be_certified():
    assert _unproven("java21", {"java21": ["rhel", "debian"]}) == set()


def test_a_family_nobody_has_built_blocks_certification():
    """THE gate. java21 proven on Oracle Linux only, offered on both."""
    assert _unproven("java21", {"java21": ["rhel"]}) == {"debian"}


def test_a_technology_with_no_evidence_at_all_blocks_certification():
    assert _unproven("java21", {}) == {"rhel", "debian"}


def test_a_disproven_family_does_not_block_certification():
    """nodejs20 on Ubuntu was built, measured at 18.19.1 and declined. The form
    refuses that image, so no requester can reach it — certifying the technology
    can only ever build the proven half."""
    assert _unproven("nodejs20", {"nodejs20": ["rhel"]},
                     refuted={"nodejs20": {"debian": "Node 18, not 20"}}) == set()


def test_evidence_for_one_technology_does_not_certify_another():
    """The blueprint builds several. Proving java21 says nothing about python312."""
    assert _unproven("python312", {"java21": ["rhel", "debian"]}) == {"rhel", "debian"}


def test_a_blueprint_that_boots_no_machine_is_not_held_to_boot_evidence():
    """A bucket or a managed database has no OS and files no report. Refusing to
    certify those would block work the gate was never about — but it IS a stated
    gap, not a silent one: proving them needs the resource asked whether it is
    healthy, which is separate work."""
    assert _unproven("oci-objectstorage", {}, families=()) == set()


# --- Unknown is not permission ------------------------------------------------

def test_unreadable_evidence_is_not_treated_as_approval(monkeypatch):
    """The failure this project keeps repeating: a check that stopped checking
    looking exactly like one that found nothing wrong. Here it would certify
    anything at all.

    The fetcher is stubbed to FAIL, and the cache cleared, so the unknown branch
    is genuinely reached. An earlier version of this test did neither — the
    shared fixture had already seeded the cache, so `known` was always True and
    the test passed while the branch it names was never executed. It caught
    nothing when the branch was deliberately broken.
    """
    caps.reset()
    monkeypatch.setattr(api_main, "_orchestrator_blueprints", lambda: None)
    assert api_main._unproven_families("java21", {"os_families": ["rhel"]}) is None


def test_the_endpoint_refuses_rather_than_certifying_on_unreadable_evidence():
    import inspect
    source = inspect.getsource(api_main.certify_blueprint)
    assert "if unproven is None:" in source
    assert "503" in source


# --- The real manifests, not a synthetic payload ------------------------------

def test_the_shipped_blueprints_actually_publish_their_evidence():
    """Every test above builds its own payload, so all of them would pass while
    the orchestrator published nothing at all — which is exactly what happened
    when this was planted. This reads what the registry really emits."""
    from orchestrator import blueprint_registry, configure
    shipped = blueprint_registry.discover()
    published = {}
    for bp in shipped:
        for code, families in (bp.get("verified") or {}).items():
            published.setdefault(code, set()).update(families)
    assert published, "the registry publishes no evidence at all"
    for code, family in configure.VERIFIED:
        if any(code in (bp.get("builds") or []) for bp in shipped):
            assert family in published.get(code, set()), (
                f"{code} is proven on {family} but the registry does not say so")


# --- The refusal has to be actionable ----------------------------------------

def test_the_gate_is_wired_into_the_endpoint():
    """A rule nothing calls is decoration — this project has shipped two of those."""
    import inspect
    source = inspect.getsource(api_main.certify_blueprint)
    assert "_unproven_families" in source
    assert "has not been proven on" in source


def test_the_refusal_says_how_to_earn_the_certificate():
    """'Not proven' leaves an admin stuck. The way forward is what makes it a
    process rather than a wall."""
    import inspect
    source = inspect.getsource(api_main.certify_blueprint)
    assert "let the machine report, then certify" in source


# --- No route may be a private helper -----------------------------------------

def test_no_route_handler_is_a_private_helper():
    """A decorator applies to whatever function follows it.

    Adding `_unproven_families` directly beneath `@app.post("/api/blueprints")`
    registered the HELPER as the endpoint and left the real one unreachable —
    ten tests failed at once, none of them near the cause. The same shape as a
    handler shadowing an imported module: invisible in review, silent until a
    request arrives.
    """
    routes = [r for r in api_main.app.routes
              if getattr(r, "endpoint", None) is not None]
    private = sorted({r.endpoint.__name__ for r in routes
                      if r.endpoint.__name__.startswith("_")
                      and r.endpoint.__module__ == api_main.__name__})
    assert not private, (
        f"these private helpers are registered as HTTP endpoints — a decorator "
        f"has attached to the wrong function: {private}")


def test_the_certify_route_points_at_the_certify_function():
    """Named separately: the guard above would still pass if the decorator landed
    on some other public function."""
    match = [r for r in api_main.app.routes
             if getattr(r, "path", "") == "/api/blueprints"
             and "POST" in getattr(r, "methods", set())]
    assert match, "the certify route is not registered at all"
    assert match[0].endpoint.__name__ == "certify_blueprint"
