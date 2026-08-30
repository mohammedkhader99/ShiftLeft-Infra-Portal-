"""A blueprint must say whether the cloud runs it or we build machines.

"PostgreSQL" and "Kafka" sit next to each other on one catalogue and arrive as
completely different things: one is a database OCI runs and never lets you near,
the other is machines somebody now owns and has to patch. Nothing on the request
form said which — a requester learned it from the bill.

DECLARED, NEVER INFERRED. The same fact was once guessed from the technology
CODE, and it was wrong in both directions: `postgres16` reads as software by that
rule and is a managed service. db/models.TechnologyDelivery was written to end
that guess for the CATALOGUE; this is the same fact about the BLUEPRINT, which is
what actually builds the thing.

WHY THE GATE IS A TEST AND NOT A RUNTIME RULE. Requiring `delivery` in
blueprint_registry would DROP any manifest without it, and a dropped blueprint
makes its technologies unprovisionable. That is exactly how the certification
gate left bare VMs unbuildable for twelve days with nobody noticing. So the
runtime reports "" and claims nothing, and the shipped set is held to the
standard here, where a gap fails a build instead of a request.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator import blueprint_registry

SHIPPED = sorted(Path("orchestrator/blueprints").glob("*.yaml"))
KNOWN = {"managed", "vm"}


def test_there_are_blueprints_to_check():
    """A glob that matches nothing passes every test below silently."""
    assert len(SHIPPED) >= 8


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_a_shipped_blueprint_says_how_it_delivers(path):
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert manifest.get("delivery") in KNOWN, (
        f"{path.name} does not say how it delivers. Add `delivery: managed` "
        f"(the cloud runs it) or `delivery: vm` (we build machines).")


def test_the_declaration_reaches_the_portal():
    """It is read by the API over the signed channel; a value that never leaves
    the manifest cannot tell a requester anything."""
    shipped = blueprint_registry.discover()
    by_ref = {bp["ref"]: bp for bp in shipped}

    assert by_ref["oci/postgres"]["delivery"] == "managed"
    assert by_ref["oci/service-vm"]["delivery"] == "vm"


def test_kafka_is_not_advertised_as_a_managed_service():
    """oci/kafka is Apache Kafka ON MACHINES. OCI Streaming is the managed
    alternative and there is no blueprint for it.

    Pinned because the temptation is real and the mistake is invisible: a form
    offering "Kafka, managed service" backed by this module would build three
    VMs, and nothing would contradict it until the bill or the patch cycle."""
    by_ref = {bp["ref"]: bp for bp in blueprint_registry.discover()}

    assert by_ref["oci/kafka"]["delivery"] == "vm"


def test_a_manifest_that_does_not_say_is_carried_as_unknown(tmp_path):
    """NOT DROPPED. A required field here would make its technologies
    unprovisionable, which is the twelve-day outage described above."""
    (tmp_path / "x.yaml").write_text(
        "ref: t/x\ntarget: oci\nresource_kind: t-x\nmodule: .\nbuilds: [x]\n",
        encoding="utf-8")

    found = blueprint_registry.discover(directory=tmp_path, generated=tmp_path / "none")

    assert [bp["ref"] for bp in found] == ["t/x"], "the blueprint was dropped"
    assert found[0]["delivery"] == ""


def test_an_unknown_word_is_carried_as_written_not_silently_corrected(tmp_path):
    """The portal shows it as unclassified rather than guessing which was meant."""
    (tmp_path / "x.yaml").write_text(
        "ref: t/x\ntarget: oci\nresource_kind: t-x\nmodule: .\nbuilds: [x]\n"
        "delivery: Serverless\n", encoding="utf-8")

    found = blueprint_registry.discover(directory=tmp_path, generated=tmp_path / "none")

    assert found[0]["delivery"] == "serverless"


def test_a_blueprint_the_agent_writes_says_it_builds_machines():
    """The agent drafts Terraform for MACHINES. It does not draft managed
    services — those are products a cloud sells, not modules anyone composes —
    and a draft claiming one would put an offering on the form that nothing can
    build."""
    from api import ai_blueprint

    manifest = ai_blueprint._NEW_SERVICE_MANIFEST.format(
        target="oci", slug="thing", kind="oci-thing", candidate="thing",
        name_var="instance_name")

    assert yaml.safe_load(manifest)["delivery"] == "vm"
