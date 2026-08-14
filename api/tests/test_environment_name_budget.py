"""An environment name must survive into the resource that gets built.

REQ-2026-0128 was approved in Jira and then failed at Terraform PLAN time: the
composed name `test121-req-2026-0128-service-vm` was 32 characters against the
module's limit of 31. Nothing had checked, because the length only becomes
knowable once you know which technologies were chosen and which blueprint builds
each of them.

The orchestrator now compresses an over-long name rather than failing — but it
compresses by trimming the ENVIRONMENT name, so `visa-preprod-eu` would appear in
OCI as `visa-preprod` and nobody searching for their machine would find it.
Shortening the REFERENCE is lossless and stays silent; trimming the environment
is lossy and is refused here, with the length that would work.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import blueprint_capabilities, validation
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
    """A seeded database, for the tests that go through validate_submission —
    it checks cost centres and technologies against real rows."""
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    yield session
    session.close()


def _errors(name: str, *codes: str) -> dict:
    errors: dict = {}
    validation._validate_environment_name_fits(
        {"environment_name": name,
         "components": [{"technology_code": c} for c in codes]},
        errors)
    return errors


# --- Refused, with the number that would work --------------------------------

def test_a_name_too_long_for_its_blueprint_is_refused():
    errors = _errors("visa-preprod-eu", "nginx")
    assert "environment_name" in errors
    message = errors["environment_name"]
    assert "15 characters" in message, "say how long it is"
    assert "12" in message, "and how long it may be"
    assert "service-vm" in message, "and why — the suffix is the hidden cost"


def test_the_message_names_the_component_responsible():
    """With a mixed stack the requester needs to know WHICH component is the
    constraint, or the only remedy they can see is 'make it shorter and guess'."""
    assert "nginx" in _errors("visa-preprod-eu", "nginx")["environment_name"]


def test_the_tightest_component_in_the_stack_decides():
    """apache allows 16 and nginx 12. A request with both must be held to 12,
    because both machines get built."""
    assert _errors("environment-13", "apache") == {}
    errors = _errors("environment-13", "apache", "nginx")
    assert "12" in errors["environment_name"]
    assert "nginx" in errors["environment_name"]


# --- Accepted, so the guard is not merely refusing everything ----------------

@pytest.mark.parametrize("name", ["test4", "test121", "vision-qmg", "egate-uat"])
def test_realistic_names_are_accepted(name):
    """The whole point of fixing name generation first: the budget went from six
    characters to twelve, so ordinary names now pass. A rule that rejected these
    would just move the failure earlier."""
    assert _errors(name, "nginx") == {}


def test_a_longer_name_is_fine_on_a_roomier_blueprint():
    """apache's suffix is shorter, so it can carry more. The rule is per
    blueprint, not one global number."""
    assert _errors("environment-13", "apache") == {}


def test_a_technology_with_no_blueprint_imposes_no_limit():
    """Nothing builds java21, so there is no resource name to overrun."""
    assert _errors("a-very-long-environment-name-indeed", "java21") == {}


def test_no_components_no_opinion():
    assert _errors("a-very-long-environment-name-indeed") == {}


def test_an_empty_name_is_left_to_the_other_validators():
    """Its absence is a different error, reported elsewhere; two messages for one
    mistake is worse than one."""
    assert _errors("", "nginx") == {}


# --- The specific request that failed ----------------------------------------

def test_the_request_that_failed_would_now_be_accepted():
    """test121 was 7 characters and produced a 32-character name. With the
    reference shortened it produces 26, and the budget is 12 — so the request
    that could not be built is now ordinary."""
    assert _errors("test121", "nginx", "apache") == {}


def test_the_rule_is_actually_wired_into_submission(db):
    """Everything above calls the validator directly, which cannot tell whether
    validate_submission still calls it. Removing that one line broke no test
    until this one existed — the same wiring gap that hid the duplicate-request
    check."""
    from api.tests.test_requests import VALID_CREATE
    data = {**VALID_CREATE,
            "environment_name": "visa-preprod-eu",
            "deployment_target": "oci",
            "components": [{"technology_code": "nginx", "size": "small"}]}
    errors = validation.validate_submission(data, db)
    assert "environment_name" in errors
    assert "12" in errors["environment_name"]


def test_a_valid_name_passes_the_whole_submission(db):
    """The other half — a rule that refused everything would also pass above."""
    from api.tests.test_requests import VALID_CREATE
    data = {**VALID_CREATE,
            "environment_name": "vision-qmg",
            "deployment_target": "oci",
            "components": [{"technology_code": "nginx", "size": "small"}]}
    assert "environment_name" not in validation.validate_submission(data, db)


def test_capabilities_unavailable_means_no_refusal(monkeypatch):
    """If the orchestrator cannot be reached we do not know any limits, and
    refusing every request on that basis would be far worse than letting
    Terraform have the last word."""
    monkeypatch.setattr(blueprint_capabilities, "name_budget",
                        lambda code, ref_len, fetcher: None)
    assert _errors("a-very-long-environment-name-indeed", "nginx") == {}
