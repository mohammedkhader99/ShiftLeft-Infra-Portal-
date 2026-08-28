"""D2d: accepting a vendor's licence is a decision, not a configuration value.

Measured from `mcr.microsoft.com/mssql/server` on 2026-08-28 — before a machine
was spent, by reading the image config this system already fetches for
`ExposedPorts`:

    Env declared : MSSQL_PID=developer, MSSQL_RPC_PORT=135
    Entrypoint   : /opt/mssql/bin/launch_sqlservr.sh

`ACCEPT_EULA` is NOT declared, and the image refuses to start without it. So the
container rung — the only rung SQL Server has — reaches a machine, starts, and
exits immediately.

Setting that variable accepts a Microsoft contract on the organisation's behalf.
AGENT-DOCTRINE is unambiguous about this class of thing: the agent recommends,
it never decides. So the acceptance is a RECORDED OPERATOR DECISION in the Admin
console, off by default, and the recipe says where it came from.

`MSSQL_PID=developer` needs nothing from us — it is the image's own default, so
Developer edition is what an unmodified image gives. Worth knowing rather than
setting: the Developer edition is free and is NOT licensed for production use,
which is a fact for the operator turning the setting on, and is why the setting's
help text says so.

Nothing here reaches the network.
"""

from __future__ import annotations

import json

import pytest

from api import ai_blueprint, settings
from common import profile_rules

MSSQL_IMAGE = {
    "image": "mcr.microsoft.com/mssql/server",
    "tag": "latest",
    "digest": "sha256:" + "d" * 64,
    "ports": [1433],
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("CONTAINER_LICENCE_ACCEPTED", raising=False)
    settings.invalidate_cache()
    yield
    settings.invalidate_cache()


def recipe(code="mssql", image=None):
    draft = ai_blueprint.draft_from_image(code, image or MSSQL_IMAGE,
                                          target="oci")
    return json.loads(next(iter(draft.files.values())))


# --- off unless a person turned it on -----------------------------------------

def test_no_licence_is_accepted_by_default():
    """THE test. A default that accepted contracts would make every later
    assertion in this file decoration."""
    assert ai_blueprint.licence_accepted() is False
    assert "environment" not in recipe()["container"]


def test_the_setting_ships_off():
    """A default lives in two places — the code and the allow-list — and this
    project has shipped a switch that said "off" in one and was on in the other
    three times (RESOLVE_BY_CONFIDENCE, GOLDEN_IMAGES, the cost cap)."""
    assert settings.ALLOWLIST["CONTAINER_LICENCE_ACCEPTED"]["default"] == "false"


def test_it_is_editable_in_the_admin_console():
    """The standing rule: every setting must surface there. A licence acceptance
    that could only be changed by a release would be the worst example of it."""
    spec = settings.ALLOWLIST["CONTAINER_LICENCE_ACCEPTED"]

    assert spec["type"] == "bool"
    assert spec["group"] == "Governance"
    assert "not licensed for production" in spec["help"], (
        "the help text does not warn that a vendor image's default edition may "
        "be a development edition")


# --- and when they have ---------------------------------------------------------

def test_a_recorded_acceptance_reaches_the_recipe(monkeypatch):
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    assert recipe()["container"]["environment"] == {"ACCEPT_EULA": "Y"}


def test_the_recipe_says_where_the_acceptance_came_from(monkeypatch):
    """An approver reading this must be able to tell that a person accepted the
    terms, not that an agent decided to."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    note = recipe()["_note"]

    assert "operator" in note.lower()
    assert "ACCEPT_EULA" in note


def test_it_is_set_for_every_image_which_is_why_there_is_no_table(monkeypatch):
    """A container that does not read the variable ignores it; one that needs it
    and does not get it dies. Harmless in one direction and fatal in the other,
    so there is no case for a list of which images care — and a list is what
    every previous version of this system got wrong."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    for code in ("rabbitmq", "mongodb", "neverheardofit"):
        assert recipe(code)["container"]["environment"] == {"ACCEPT_EULA": "Y"}


def test_the_recipe_is_still_one_the_machine_will_accept(monkeypatch):
    """`environment` reaches a systemd unit read by root. It goes through the
    same validation as every other field, and a recipe that fails it is never
    published — so a licence acceptance must not be what breaks one."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    assert profile_rules.profile_problems(recipe()) == []


def test_turning_it_off_again_removes_it(monkeypatch):
    """A revoked acceptance must stop reaching machines. Recipes drafted while
    it was on are a separate matter — they are proved and certified artefacts,
    and withdrawing those is the operator's call."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "false")
    settings.invalidate_cache()

    assert "environment" not in recipe()["container"]
