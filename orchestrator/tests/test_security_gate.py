"""Security rules for what a passing build cannot tell you (C5b).

ARCHITECTURE.md §8: a passing proof says a module BUILDS, not that it is SAFE.
Public exposure, encryption, tagging and credential handling are all invisible to
a green apply — the OKE bastion carried assign_public_ip = true and built
correctly for months.

The scanner already existed and was trusted, but it had rules for exactly one
resource type. Everything else returned a clean pass having been checked for
nothing, which is tolerable for recipes people wrote and reviewed and is exactly
wrong for one a model wrote.
"""

from __future__ import annotations

from orchestrator import scanner


def plan(rtype, after, actions=("create",)):
    return {"resource_changes": [
        {"type": rtype, "address": f"{rtype}.env",
         "change": {"actions": list(actions), "after": after}}]}


TAGS = {t: "x" for t in scanner.MANDATORY_TAGS}


# --- The exposures a green build is silent about -----------------------------

def test_a_public_instance_is_a_high_finding():
    """The OKE bastion, exactly. It built perfectly for months."""
    out = scanner.scan_plan(plan("oci_core_instance", {
        "create_vnic_details": [{"assign_public_ip": True}], "freeform_tags": TAGS}))
    assert [f["rule"] for f in out["findings"]] == ["instance-public-ip"]
    assert out["ok"] is False


def test_a_private_instance_passes():
    out = scanner.scan_plan(plan("oci_core_instance", {
        "create_vnic_details": [{"assign_public_ip": False}], "freeform_tags": TAGS}))
    assert out["findings"] == [] and out["ok"] is True


def test_a_public_kubernetes_api_is_a_high_finding():
    out = scanner.scan_plan(plan("oci_containerengine_cluster", {
        "endpoint_config": [{"is_public_ip_enabled": True}]}))
    assert [f["rule"] for f in out["findings"]] == ["cluster-public-api"]


def test_a_database_password_that_is_not_a_vault_reference_is_high():
    """The portal holds a secret's OCID, never its value. A generated module that
    took a password as a variable would build fine and leak it into state."""
    out = scanner.scan_plan(plan("oci_psql_db_system", {
        "credentials": [{"password_details": [{"password_type": "PLAIN_TEXT"}]}],
        "freeform_tags": TAGS}))
    assert "psql-inline-password" in [f["rule"] for f in out["findings"]]


def test_a_vault_referenced_password_passes():
    out = scanner.scan_plan(plan("oci_psql_db_system", {
        "credentials": [{"password_details": [{"password_type": "VAULT_SECRET"}]}],
        "freeform_tags": TAGS}))
    assert out["findings"] == []


def test_untagged_resources_are_reported():
    """Tags are how anything is attributed to a cost centre or torn down later."""
    out = scanner.scan_plan(plan("oci_core_instance", {
        "create_vnic_details": [{"assign_public_ip": False}], "freeform_tags": {}}))
    assert "instance-missing-tags" in [f["rule"] for f in out["findings"]]


def test_deletes_are_not_scanned():
    """A resource being destroyed carries no new misconfiguration."""
    out = scanner.scan_plan(plan("oci_core_instance",
                                 {"create_vnic_details": [{"assign_public_ip": True}]},
                                 actions=("delete",)))
    assert out["findings"] == []


# --- The gap that mattered most ----------------------------------------------

def test_an_unknown_resource_type_is_reported_even_when_not_strict():
    """Measurable before anyone relies on it: the scanner should be able to say
    what it did NOT check."""
    out = scanner.scan_plan(plan("oci_brand_new_thing", {}))
    assert out["unreviewed_types"] == ["oci_brand_new_thing"]


def test_an_unchecked_type_is_a_clean_pass_for_a_reviewed_module():
    """Lenient by default. People wrote and reviewed the shipped recipes, and
    turning every unfamiliar resource into a blocker there would stop the portal
    provisioning anything."""
    out = scanner.scan_plan(plan("oci_brand_new_thing", {}))
    assert out["ok"] is True
    assert out["findings"] == []


def test_an_unchecked_type_is_a_BLOCKER_for_an_agent_written_module():
    """THE point of strict mode. 'We have no rules for this' must never read as
    'this is safe' when nobody reviewed the module — a generated recipe full of
    unfamiliar resource types would otherwise score a clean pass having been
    checked for nothing at all."""
    out = scanner.scan_plan(plan("oci_brand_new_thing", {}), strict=True)
    assert out["ok"] is False
    finding = out["findings"][0]
    assert finding["rule"] == "unreviewed-resource-type"
    assert finding["severity"] == "high"
    assert "not a pass" in finding["message"]


def test_strict_mode_still_applies_the_real_rules():
    """Strict must not replace the checks with a blanket refusal — a known type
    is still checked properly."""
    out = scanner.scan_plan(plan("oci_core_instance", {
        "create_vnic_details": [{"assign_public_ip": False}],
        "freeform_tags": TAGS}), strict=True)
    assert out["ok"] is True, out["findings"]
