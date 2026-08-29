"""REQ-2026-0234 certified a module whose first line says it builds nothing.

OCI Functions had no module, so the drafter produced its scaffold — and wrote,
in the file itself:

    # DRAFT — not reviewed, not certified, builds nothing yet.
    resource "TODO_provider_resource" "env" {
      count = var.resource_kind == "oci-oci-functions" ? 1 : 0

`TODO_provider_resource` is not a Terraform resource type, and the count is
zero, so the module PLANS AND APPLIES CLEANLY and creates nothing:

    apply.completed: oci-oci-functions: Apply complete! Resources: 0 added.

The proof passed, oci-functions was CERTIFIED, and the request was reported
`provisioned` with an active resource recorded that had never existed.

Every honest signal was present and none was read: the draft's own first line,
its kind, and its reasoning ("a skeleton rather than a working recipe").

JUDGED ON THE FILES, NOT ON THE KIND — and that distinction is the whole care
in this fix. `new-service` covers TWO drafts: the deterministic scaffold, and a
module THE MODEL ACTUALLY WROTE. Refusing the kind would have removed the
agent's ability to write Terraform at all, which is the reviewer's standing
requirement and is live. What disqualifies a module is that it declares nothing
real, whoever produced it.

Nothing here reaches a cloud.
"""

from __future__ import annotations

import pytest

from api import ai_blueprint

REAL = ('resource "oci_functions_application" "app" {\n'
        '  compartment_id = var.compartment_ocid\n'
        '}\n')


def test_the_scaffold_is_refused():
    """THE test, against the real scaffold rather than a hand-written copy of
    it — so a change to the scaffold cannot quietly escape this."""
    scaffold = ai_blueprint._NEW_SERVICE_SCAFFOLD.format(
        candidate="oci-functions", kind="oci-oci-functions",
        name_var="oci_functions_name")

    findings = ai_blueprint.review_module({"main.tf": scaffold})

    assert [f.rule for f in findings] == ["placeholder-resource"], findings
    assert findings[0].severity == "blocker"


def test_a_module_the_model_actually_wrote_is_accepted():
    """THE GUARD THAT MATTERS. The agent writing its own Terraform is a standing
    requirement and it is switched on. This check must catch a skeleton without
    touching a real module."""
    assert ai_blueprint.review_module({"main.tf": REAL}) == []


def test_a_module_with_no_resources_at_all_is_refused():
    """Proving it would establish only that Terraform can run."""
    findings = ai_blueprint.review_module({"main.tf": 'variable "x" {}\n'})

    assert [f.rule for f in findings] == ["builds-nothing"]


def test_other_placeholder_spellings_are_caught():
    for name in ("TODO_provider_resource", "PLACEHOLDER_thing", "changeme_thing"):
        body = f'resource "{name}" "env" {{\n}}\n'
        assert ai_blueprint.review_module({"main.tf": body}), name


def test_a_draft_with_no_terraform_is_not_judged():
    """A technology profile is not a module. Judging it here would refuse every
    vm-service recipe in the catalogue."""
    assert ai_blueprint.review_module({"generated/profiles/x.json": "{}"}) == []
    assert ai_blueprint.review_module({}) == []


def test_a_real_resource_beside_a_placeholder_is_still_refused():
    """Half a module is not a module. A scaffold that grew one real resource is
    still a starting point for a person."""
    body = REAL + '\nresource "TODO_provider_resource" "env" {\n}\n'

    assert ai_blueprint.review_module({"main.tf": body})


# --- and the gate that reads it -------------------------------------------------

def test_build_refuses_the_scaffold_before_spending_a_machine(monkeypatch):
    """The finding has to reach the gate. `review_module` being right and
    unread is exactly the shape of the defect it exists to stop."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api import autobuild
    from db.seed import seed
    from db.session import Base

    monkeypatch.setenv("AUTOBUILD_ENABLED", "true")
    monkeypatch.setenv("AI_MODE", "mock")
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(db)
    db.commit()

    proved: list = []
    result = autobuild.build(
        "oci-newthing", db, blueprint=None,
        run_proof=lambda s, b: proved.append(1),
        publish=lambda files: list(files), target="oci")

    assert proved == [], "a machine was spent proving a module that builds nothing"
    assert result.status != "published", result.detail
    assert any(a.stage == "linted" for a in result.attempts), (
        [a.stage for a in result.attempts])
    db.close()
