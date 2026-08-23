"""A machine that builds is not a recipe that installs (found QA'ing REQ-2026-0185).

`oci/service-vm` declares `os_families: [rhel, debian]` and that is true of the
BLUEPRINT: it builds machines, resolves either image from OCI, and configures
both. The portal read that as the answer to a different question — which
operating systems the SOFTWARE on the machine can be installed on — and the two
diverge the moment an agent writes a profile with one family block.

HashiCorp Vault's profile has an `rhel` block and nothing else. The form offered
Ubuntu images for it, `validate()` accepted one, the request was priced,
approved, and a real VM was paid for. Then the machine reported:

    not_installable_on_debian=vault
    PORTAL FAILURE: vault has no install recipe for debian; nothing was
    installed for it

The proof cannot catch this. A proof carries no image, so certification is
earned on rhel and says nothing about the family the requester picked — which
means the standing rule (a certified component must not fail when selected) is
broken by the form, not by the recipe.

The recipe knows the answer. `configure.supported_families` reads the family
blocks straight off the profile. It lives in the orchestrator and the portal
cannot import it, so it travels in the manifest as `installs_on`, the same way
`needs_internet` and `refuted` already do.
"""

from __future__ import annotations

from api import blueprint_capabilities as bc

SERVICE_VM = {
    "ref": "oci/service-vm", "target": "oci", "resource_kind": "oci-service-vm",
    "os_families": ["rhel", "debian"],
    "builds": ["nginx", "vault", "nodejs20", "somethingnew"],
    "installs_on": {"nginx": ["debian", "rhel"], "vault": ["rhel"],
                    "nodejs20": ["debian", "rhel"]},
    "refuted": {"nodejs20": {"debian": "only a third-party apt source; declined"}},
}


def test_software_with_one_family_block_is_offered_one_family():
    """THE defect. Vault's recipe is Red Hat only, whatever the machine can boot."""
    assert bc._build([SERVICE_VM])["vault"] == {"rhel"}


def test_software_with_a_recipe_for_both_still_gets_both():
    """The narrowing must not become a blanket refusal — nginx genuinely
    installs on either."""
    assert bc._build([SERVICE_VM])["nginx"] == {"rhel", "debian"}


def test_a_technology_that_makes_no_claim_keeps_what_the_blueprint_said():
    """`installs_on` is a narrowing, never a widening, and a code absent from it
    must behave exactly as it did before this existed. Otherwise adding the key
    would silently empty the dropdown for everything the orchestrator could not
    describe — the failure mode of hiding a legitimate image over OUR gap."""
    assert bc._build([SERVICE_VM])["somethingnew"] == {"rhel", "debian"}


def test_a_machines_refusal_still_wins_over_a_recipe_that_exists():
    """nodejs20 HAS a debian recipe. A machine disproved it anyway, and evidence
    beats the existence of a recipe."""
    assert bc._build([SERVICE_VM])["nodejs20"] == {"rhel"}


def test_a_manifest_without_the_key_at_all_is_unchanged():
    """Older orchestrators, and every blueprint that ships no profiles."""
    older = {k: v for k, v in SERVICE_VM.items() if k != "installs_on"}
    assert bc._build([older])["vault"] == {"rhel", "debian"}


def test_the_narrowing_cannot_widen_beyond_the_blueprint():
    """A profile claiming a family the blueprint cannot configure at all must not
    put that family on the form — cloud-init would have no idea what to do."""
    odd = {**SERVICE_VM, "os_families": ["rhel"],
           "installs_on": {"nginx": ["rhel", "debian", "suse"]}}
    assert bc._build([odd])["nginx"] == {"rhel"}
