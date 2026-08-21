"""Agent-written blueprints live apart from reviewed ones, and stay apart (C5a).

The portal's drafter may now write a Terraform module and manifest. For that loop
to close at all they have to land somewhere the orchestrator can both discover
and build from, which the image cannot be — a module written into it at run time
would be invisible and would vanish on the next restart. So there is a mounted
store.

The whole risk of that store is impersonation. A generated blueprint that could
take the ref or resource kind of a reviewed one would inherit trust nobody
granted it, and no status page would show anything wrong. These tests are that
boundary.
"""

from __future__ import annotations

import pytest

from orchestrator import blueprint_registry as br

SHIPPED = """ref: oci/postgres
target: oci
resource_kind: oci-postgres
module: .
builds: [postgres16]
"""

GENERATED = """ref: oci/cassandra
target: oci
resource_kind: oci-cassandra
module: oci/cassandra
builds: [cassandra5]
"""


@pytest.fixture()
def roots(tmp_path):
    shipped = tmp_path / "blueprints"
    generated = tmp_path / "generated"
    shipped.mkdir()
    generated.mkdir()
    (shipped / "oci-postgres.yaml").write_text(SHIPPED, encoding="utf-8")
    return shipped, generated


def find(entries, ref):
    return next((e for e in entries if e["ref"] == ref), None)


# --- The store is discovered at all ------------------------------------------

def test_a_generated_blueprint_is_discovered(roots):
    shipped, generated = roots
    (generated / "oci-cassandra.yaml").write_text(GENERATED, encoding="utf-8")

    found = br.discover(shipped, generated)
    entry = find(found, "oci/cassandra")
    assert entry is not None, "the mounted store was not read"
    assert entry["resource_kind"] == "oci-cassandra"


def test_every_entry_says_where_it_came_from(roots):
    """Origin travels with the entry. A reviewer looking at the blueprint list
    must be able to tell, without leaving the page, which of these a person
    approved and which a model wrote."""
    shipped, generated = roots
    (generated / "oci-cassandra.yaml").write_text(GENERATED, encoding="utf-8")

    found = br.discover(shipped, generated)
    assert find(found, "oci/postgres")["origin"] == "shipped"
    assert find(found, "oci/cassandra")["origin"] == "generated"


# --- Impersonation: the boundary that matters --------------------------------

def test_a_generated_blueprint_may_not_take_a_shipped_refs_name(roots):
    """THE risk. Shadowing a reviewed blueprint would inherit trust nobody
    granted, and nothing on a status page would look wrong."""
    shipped, generated = roots
    (generated / "impostor.yaml").write_text(SHIPPED, encoding="utf-8")

    found = br.discover(shipped, generated)
    entries = [e for e in found if e["ref"] == "oci/postgres"]
    assert len(entries) == 2, entries
    real = next(e for e in entries if e["origin"] == "shipped")
    fake = next(e for e in entries if e["origin"] == "generated")
    assert "error" not in real, "the reviewed blueprint was affected by the impostor"
    assert "may not shadow it" in fake["error"]
    assert fake["builds"] == [], "the impostor was still offered as buildable"


def test_a_generated_blueprint_may_not_reuse_a_shipped_resource_kind(roots):
    """A different ref pointing at the same resource kind is the same attack: the
    provisioning path keys on resource_kind, not on ref."""
    shipped, generated = roots
    (generated / "sneaky.yaml").write_text(
        "ref: oci/postgres-v2\ntarget: oci\nresource_kind: oci-postgres\n"
        "module: oci/pg2\nbuilds: [postgres17]\n", encoding="utf-8")

    found = br.discover(shipped, generated)
    fake = find(found, "oci/postgres-v2")
    assert "may not shadow it" in fake["error"]


def test_a_collision_is_reported_not_silently_dropped(roots):
    """Dropping it would leave the agent believing it had published something,
    and a human wondering where it went."""
    shipped, generated = roots
    (generated / "impostor.yaml").write_text(SHIPPED, encoding="utf-8")

    found = br.discover(shipped, generated)
    assert any(e.get("error") and e["origin"] == "generated" for e in found)


# --- Nothing else changes ----------------------------------------------------

def test_an_empty_store_changes_nothing(roots):
    """The common case: no agent has written anything yet."""
    shipped, generated = roots
    found = br.discover(shipped, generated)
    assert [e["ref"] for e in found] == ["oci/postgres"]
    assert found[0]["origin"] == "shipped"


def test_a_missing_store_is_not_an_error(roots, tmp_path):
    """The volume may simply not be mounted. Discovery must not fail closed —
    that would read as 'the orchestrator ships nothing'."""
    shipped, _ = roots
    found = br.discover(shipped, tmp_path / "does-not-exist")
    assert [e["ref"] for e in found] == ["oci/postgres"]


def test_a_broken_generated_manifest_is_attributed_correctly(roots):
    """A parse failure must not read as though it shipped with the product."""
    shipped, generated = roots
    (generated / "broken.yaml").write_text("ref: [unclosed\n", encoding="utf-8")

    found = br.discover(shipped, generated)
    broken = next(e for e in found if e.get("error"))
    assert broken["origin"] == "generated"


# --- The module path cannot escape its own root ------------------------------

def test_a_generated_module_path_cannot_reach_a_shipped_module(monkeypatch, tmp_path):
    """`module: ../../terraform/oci/oke` would let a generated manifest point at
    a reviewed module and inherit its trust — the path-traversal version of the
    impersonation above."""
    from orchestrator import provisioner

    gen_root = tmp_path / "generated" / "terraform"
    (gen_root / "oci" / "cassandra").mkdir(parents=True)
    monkeypatch.setattr(br, "GENERATED_MODULE_ROOT", gen_root)
    monkeypatch.setattr(provisioner.blueprint_registry, "GENERATED_MODULE_ROOT", gen_root)

    monkeypatch.setattr(provisioner.blueprint_registry, "for_resource_kind",
                        lambda kind: {"origin": "generated", "module": "../../../etc"})
    escaped = provisioner._module_dir("oci", "oci-cassandra")
    assert not str(escaped).endswith("etc"), f"escaped its root: {escaped}"

    monkeypatch.setattr(provisioner.blueprint_registry, "for_resource_kind",
                        lambda kind: {"origin": "generated", "module": "oci/cassandra"})
    ok = provisioner._module_dir("oci", "oci-cassandra")
    assert ok == (gen_root / "oci" / "cassandra").resolve()


def test_a_shipped_manifest_still_resolves_inside_the_image(monkeypatch):
    """Origin decides the root. A shipped blueprint must not start resolving
    into the mounted store just because the store exists."""
    from orchestrator import provisioner

    monkeypatch.setattr(provisioner.blueprint_registry, "for_resource_kind",
                        lambda kind: {"origin": "shipped", "module": "oci/oke"})
    resolved = provisioner._module_dir("oci", "oci-oke")
    assert resolved == (provisioner.MODULE_DIR / "oci" / "oke").resolve()
