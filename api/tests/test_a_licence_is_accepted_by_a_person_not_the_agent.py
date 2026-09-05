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

#: SQL Server after its first machine has spoken: the log named ACCEPT_EULA,
#: the operator's recorded decision allowed it, and `container_env` handed it
#: back. Since 2026-09-05 that is the only way a licence reaches a recipe.
MSSQL_ASKED = {**MSSQL_IMAGE, "environment": {"ACCEPT_EULA": "Y"}}


def test_a_recorded_acceptance_reaches_the_recipe(monkeypatch):
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    assert recipe("mssql", MSSQL_ASKED)["container"]["environment"] == {
        "ACCEPT_EULA": "Y"}


def test_the_recipe_says_where_the_acceptance_came_from(monkeypatch):
    """An approver reading this must be able to tell that a person accepted the
    terms, not that an agent decided to."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    note = recipe("mssql", MSSQL_ASKED)["_note"]

    assert "operator" in note.lower()
    assert "ACCEPT_EULA" in note


def test_it_is_not_set_for_an_image_that_never_asked_for_it(monkeypatch):
    """CHANGED 2026-09-05, and the reasoning it replaces is worth keeping.

    This used to be `test_it_is_set_for_every_image_which_is_why_there_is_no_table`:
    a container that does not read the variable ignores it, one that needs it and
    does not get it dies, harmless one way and fatal the other — so it was set on
    everything rather than guessed from a list, because a hand-curated list of
    which images care is what every earlier version of this system got wrong.

    That was the right call while there was no third option. C9 built one: when a
    container will not start, the machine captures its log, and the log NAMES the
    variables the image demands. Evidence, not a list and not a blanket.

    The blanket's cost was visible the day MySQL was certified. `library/mysql`
    is GPL, never reads ACCEPT_EULA, and its recipe carried
    `ACCEPT_EULA=Y` with a note reading "an operator has recorded acceptance of
    vendor licence terms" — a licence claim on a technology that requires no
    licence, in an artefact an approver reads.
    """
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    for code in ("rabbitmq", "mongodb", "mysql", "neverheardofit"):
        assert "environment" not in recipe(code)["container"], (
            f"{code} was given a licence acceptance it never asked for")


def test_it_is_set_when_the_container_asked_for_it(monkeypatch):
    """The path that matters. `container_env` reads ACCEPT_EULA out of the
    machine's own log, checks the operator's recorded decision, and hands it
    back; the drafter carries what it is given."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()
    asked = {**MSSQL_IMAGE, "environment": {"ACCEPT_EULA": "Y"}}

    assert recipe("mssql", asked)["container"]["environment"] == {"ACCEPT_EULA": "Y"}


def test_the_note_claims_no_licence_that_was_not_asked_for(monkeypatch):
    """The note is what a person reads to understand the recipe. Saying a
    licence was accepted, for an image that never wanted one, is the portal
    telling an approver something untrue about a contract."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    note = recipe("mysql")["_note"]

    assert "ACCEPT_EULA" not in note
    assert "licence" not in note.lower()


def test_a_recipe_that_asked_for_a_PASSWORD_claims_no_licence(monkeypatch):
    """FOUND BY A PLANT, 2026-09-05, and it is the shape MySQL actually has.

    The first version of this checked `recipe("mysql")` -- an image that asked
    for NOTHING, so the whole clause was skipped and a note claiming a licence
    for every recipe with an environment went unnoticed. The real MySQL recipe
    has an environment: MYSQL_RANDOM_ROOT_PASSWORD, and no licence. That is the
    case where a false licence claim would actually be written."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()
    asked = {**MSSQL_IMAGE, "image": "docker.io/library/mysql",
             "environment": {"MYSQL_RANDOM_ROOT_PASSWORD": "yes"}}

    out = recipe("mysql", asked)

    assert out["container"]["environment"] == {"MYSQL_RANDOM_ROOT_PASSWORD": "yes"}
    assert "MYSQL_RANDOM_ROOT_PASSWORD" in out["_note"]
    assert "ACCEPT_EULA" not in out["_note"]
    assert "licence" not in out["_note"].lower(), (
        "the recipe claims a licence acceptance for an image that asked for a "
        "password and nothing else")


def test_an_operator_who_has_not_accepted_gets_nothing_even_if_it_was_asked(monkeypatch):
    """`container_env` is what decides, and it will not return ACCEPT_EULA
    unless the setting is on — but the drafter must not be the only thing
    standing between a demand and a contract either."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "false")
    settings.invalidate_cache()
    from api import container_env

    answer = container_env.answer(["ACCEPT_EULA"], licence_accepted=False, code="mssql")

    assert answer.environment == {}
    assert answer.needs_a_person == ["ACCEPT_EULA"]


def test_the_recipe_is_still_one_the_machine_will_accept(monkeypatch):
    """`environment` reaches a systemd unit read by root. It goes through the
    same validation as every other field, and a recipe that fails it is never
    published — so a licence acceptance must not be what breaks one."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "true")
    settings.invalidate_cache()

    # Driven with an image that ASKED, or this asserts nothing about the
    # environment field: since 2026-09-05 an image that did not ask has none.
    assert profile_rules.profile_problems(recipe("mssql", MSSQL_ASKED)) == []


def test_turning_it_off_again_removes_it(monkeypatch):
    """A revoked acceptance must stop reaching machines. Recipes drafted while
    it was on are a separate matter — they are proved and certified artefacts,
    and withdrawing those is the operator's call."""
    monkeypatch.setenv("CONTAINER_LICENCE_ACCEPTED", "false")
    settings.invalidate_cache()

    # `container_env` is what withholds it when the setting is off, so the
    # image here carries nothing -- which is what a revoked acceptance means
    # by the time the drafter sees it.
    assert "environment" not in recipe()["container"]
