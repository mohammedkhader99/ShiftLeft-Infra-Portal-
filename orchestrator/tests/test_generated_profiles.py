"""The agent teaches the proven module a new technology (C6).

Most components with no recipe do not need new Terraform. keycloak, kafka,
mongodb, vault and their like are SOFTWARE ON A MACHINE, and `oci/service-vm`
already builds machines properly: it resolves the image from OCI at run time
filtered by shape, keeps the instance on a private subnet, enables Run Command
so the machine can be interrogated, and makes it report what it became. Every one
of those behaviours was paid for by a failure on a real request.

What such a component actually needs is a package, a systemd unit and a port —
what that module's own manifest calls "a data change". Writing it a second
Terraform module would duplicate a working one AND collide on its resource kind,
which is the failure the registry refuses.

So the agent writes a PROFILE, and it extends the shipped blueprint rather than
competing with it. Two gates have to learn the new code, and missing either one
is silent: the blueprint's `builds` list (or `_components_for` drops it before
configuration is even considered) and configure.py's template (or there is no
recipe to render).
"""

from __future__ import annotations

import json

import pytest

from orchestrator import blueprint_registry, configure

KEYCLOAK = {
    "code": "keycloak",
    "builds_on": "oci/service-vm",
    "ports": [8080],
    "expects": "26",
    "version_command": "/opt/keycloak/bin/kc.sh --version 2>&1",
    "rhel": {"packages": ["keycloak"], "services": ["keycloak"]},
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", d)
    return d


def write(store, profile, name=None):
    (store / f"{name or profile['code']}.json").write_text(
        json.dumps(profile), encoding="utf-8")


# --- gate 1: configure.py must carry a recipe --------------------------------

def test_a_generated_profile_becomes_an_installable_recipe(store):
    write(store, KEYCLOAK)
    prof = configure.profile_for("keycloak", "rhel")

    assert prof is not None, "the machine would be given nothing to install"
    assert prof["packages"] == ["keycloak"]
    assert prof["services"] == ["keycloak"]
    assert prof["ports"] == [8080]


def test_it_appears_among_the_configurable_codes(store):
    write(store, KEYCLOAK)
    assert "keycloak" in configure.configurable_codes()


def test_a_family_the_profile_does_not_cover_is_refused_not_guessed(store):
    """Falling back to Red Hat package names on an Ubuntu machine is the exact
    failure profile_for's signature exists to prevent."""
    write(store, KEYCLOAK)
    assert configure.profile_for("keycloak", "debian") is None


# --- shipped always wins -----------------------------------------------------

def test_a_generated_profile_may_not_shadow_a_shipped_one(store):
    """nginx has a profile somebody checked against a real image. An agent
    proposing a different one must not be able to take its place — the same rule
    the blueprint registry applies to manifests, for the same reason."""
    write(store, {"code": "nginx", "builds_on": "oci/service-vm",
                  "ports": [9999], "rhel": {"packages": ["not-nginx"],
                                            "services": ["not-nginx"]}})
    prof = configure.profile_for("nginx", "rhel")
    assert prof["packages"] == ["nginx"], "an agent overwrote a reviewed recipe"
    assert 9999 not in prof["ports"]


def test_an_operator_override_still_wins_over_both(store, monkeypatch):
    """CONFIG_PACKAGE_MAP is how a live image gets corrected without a deploy.
    It has to outrank a generated profile or that escape hatch is gone."""
    write(store, KEYCLOAK)
    monkeypatch.setenv("CONFIG_PACKAGE_MAP",
                       json.dumps({"keycloak": {"packages": ["keycloak-26"],
                                                "services": ["keycloak"]}}))
    assert configure.profile_for("keycloak", "rhel")["packages"] == ["keycloak-26"]


# --- a broken draft must not take the catalogue down -------------------------

def test_malformed_json_is_skipped_not_raised(store):
    (store / "broken.json").write_text("{not json at all", encoding="utf-8")
    write(store, KEYCLOAK)
    assert configure.profile_for("keycloak", "rhel") is not None


def test_a_missing_store_is_empty_rather_than_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path / "nope")
    assert configure._generated_profiles() == {}
    assert configure.profile_for("nginx", "rhel") is not None


# --- gate 2: the blueprint's builds list -------------------------------------

def test_the_profile_extends_the_shipped_blueprints_builds(store):
    """THE gate that is silent when missed. A component absent from `builds` is
    dropped by _components_for before first-boot configuration is even
    considered, so the machine boots bare and reports success."""
    write(store, KEYCLOAK)
    vm = next(b for b in blueprint_registry.discover()
              if b.get("ref") == "oci/service-vm")

    assert "keycloak" in vm["builds"]
    assert "nginx" in vm["builds"], "it replaced the manifest's own list"


def test_what_the_agent_added_is_reported_separately(store):
    """A reader must always be able to tell what a person put on this blueprint
    from what a machine added to it."""
    write(store, KEYCLOAK)
    vm = next(b for b in blueprint_registry.discover()
              if b.get("ref") == "oci/service-vm")

    assert vm["builds_generated"] == ["keycloak"]
    assert "nginx" not in vm["builds_generated"]


def test_a_profile_naming_no_blueprint_extends_nothing(store):
    write(store, {**KEYCLOAK, "builds_on": ""})
    for b in blueprint_registry.discover():
        assert "keycloak" not in b["builds"]
