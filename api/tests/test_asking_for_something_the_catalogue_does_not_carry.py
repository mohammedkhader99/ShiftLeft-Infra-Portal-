"""F1: a requester can ask about a technology the form does not offer.

Until now there was no way to ask. `_validate_components` answers "Unknown
technology 'clickhouse'" and stops, so a requester whose software is not on the
form learns only that it is not on the form -- not whether it could be, not what
it would take, and not that the portal already knows how to find out.

THE MACHINERY EXISTED AND ONLY AN ADMIN COULD REACH IT. `install_methods` walks
every rung of the ladder and `_why_nothing_works` says what each one answered;
both sat behind /api/catalogue/autobuild, which requires manage_settings and
BUILDS THINGS. Asking a question needed neither.

READ-ONLY, and every test below is really about that. It creates no catalogue
row, raises no request, starts no build and spends nothing: a typo must not
leave a technology behind it, and "can you?" must never be a way to make the
portal do something.

THE TEXT BOX FEEDS A PATH THAT RUNS AS ROOT, which is why the name is narrowed
to the shape a catalogue code may take before it reaches a registry. The trust
rule still decides what may be pulled; this only decides what may be asked
about.
"""

from __future__ import annotations

import pytest

from api.main import _as_candidate_name


# --- what a requester may type ---------------------------------------------------

@pytest.mark.parametrize("typed, expected", [
    ("mysql", "mysql"),
    ("MySQL", "mysql"),                     # people capitalise product names
    ("  clickhouse  ", "clickhouse"),
    ("Apache Kafka", "apache-kafka"),       # a real two-word name
    ("Oracle Database Free", "oracle-database-free"),
])
def test_a_product_name_survives_being_typed_by_a_person(typed, expected):
    assert _as_candidate_name(typed) == expected


@pytest.mark.parametrize("typed", [
    "",
    "   ",
    "../etc/passwd",                        # traversal, into a registry path
    "foo/bar",                              # a path, not a name
    "redis;rm -rf /",                       # punctuation that is not a name
    "a" * 60,                               # longer than a catalogue code
    "-leading-hyphen",
    "we need a database for the new billing system please",
])
def test_what_is_not_a_name_is_refused(typed):
    """Refused BEFORE it reaches a registry. The value is used to build image
    references and package queries, so the narrowing has to happen here rather
    than being trusted to whatever consumes it."""
    assert _as_candidate_name(typed) == ""


def test_a_sentence_is_turned_away_rather_than_searched_for():
    """`_as_candidate_name` would happily hyphenate prose into something that
    matches the code pattern, and it would then be carried to a registry as a
    query that cannot match anything. A name is at most a few words."""
    assert _as_candidate_name("a description of what I want really") == ""
    assert _as_candidate_name("Oracle Database Free") != "", (
        "the bound is too tight for a real product name")


def test_the_same_rule_as_the_rest_of_the_portal():
    """Not a looser pattern invented for a text box. If CODE changes, this moves
    with it — a second, more permissive definition of 'a name' is exactly how
    something reaches a registry that the rest of the portal would refuse."""
    from common import profile_rules

    for typed in ("mysql", "apache kafka", "clickhouse"):
        out = _as_candidate_name(typed)
        assert out == "" or profile_rules.CODE.match(out), out


# --- what the endpoint answers ---------------------------------------------------

from sqlalchemy import create_engine, select                       # noqa: E402
from sqlalchemy.orm import sessionmaker                            # noqa: E402
from sqlalchemy.pool import StaticPool                             # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

from api.main import app, get_session                              # noqa: E402
from db.models import Technology                                   # noqa: E402
from db.seed import seed                                           # noqa: E402
from db.session import Base                                        # noqa: E402

ASK = "/api/catalogue/can-we-build"


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    yield session
    session.close()


@pytest.fixture()
def client(db):
    def override():
        yield db
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_something_already_in_the_catalogue_is_answered_without_the_network(
        client, monkeypatch):
    """The commonest case by far — someone did not spot it on the form — and it
    must not cost a registry round trip to say so."""
    import api.registry as registry_mod
    monkeypatch.setattr(registry_mod, "find", lambda *a, **k: pytest.fail(
        "the registry was asked about a technology we already carry"))

    body = client.get(ASK, params={"name": "PostgreSQL 16"}).json()

    assert body["known"] is True, (
        "the catalogue calls it postgres16 and the requester typed its real "
        "name — being told we do not have it is the exact failure this "
        "endpoint exists to prevent")
    assert body["reason"]


@pytest.mark.parametrize("typed, code", [
    ("PostgreSQL 16", "postgres16"),
    ("SQL Server 2022", "mssql"),
    ("Oracle Database 19c", "oracle-db"),
    ("postgres16", "postgres16"),
])
def test_a_product_name_finds_the_code_we_file_it_under(client, typed, code):
    """Someone types what the vendor calls it; we file it under something
    shorter. That mismatch IS why they could not find it on the form."""
    body = client.get(ASK, params={"name": typed}).json()

    assert body["known"] is True, f"{typed!r} did not find {code}"


def test_prose_is_refused_with_advice_rather_than_searched_for(client):
    body = client.get(ASK, params={"name": "we need a database for billing"}).json()

    assert body["known"] is False and body["buildable"] is False
    assert body["name"] == ""
    assert "name" in body["reason"].lower(), body["reason"]


def test_asking_creates_nothing(client, db):
    """THE POINT OF THE WHOLE ENDPOINT BEING A GET. A typo must not leave a
    technology behind it, and asking must never be a way to make the portal
    build something."""
    before = {t.code for t in db.scalars(select(Technology)).all()}

    for name in ("clickhouse", "notathing", "mysql", "Apache Kafka"):
        client.get(ASK, params={"name": name})

    after = {t.code for t in db.scalars(select(Technology)).all()}
    assert after == before, f"asking created {sorted(after - before)}"


def test_it_reports_the_image_it_would_pin(client, monkeypatch):
    """And says plainly that nothing is built yet, because a resolved image is
    not a proved recipe — that distinction is the whole certification model."""
    import api.registry as registry_mod
    monkeypatch.setattr(registry_mod, "find", lambda code, *a, **k: {
        "image": "docker.io/library/clickhouse", "digest": "sha256:" + "c" * 64,
        "ports": [8123, 9000]})

    body = client.get(ASK, params={"name": "clickhouse"}).json()

    assert body["known"] is False
    assert body["buildable"] is True
    assert body["image"] == "docker.io/library/clickhouse"
    assert body["digest"].startswith("sha256:")
    assert body["ports"] == [8123, 9000]
    assert "proves" in body["reason"] or "prove" in body["reason"], (
        "it does not say the thing still has to be built and proved")


def test_a_dead_end_explains_every_rung(client, monkeypatch):
    """The refusal a requester gets must be the one the agent would give. Two
    explanations of one mechanism is how a portal starts lying slowly."""
    import api.registry as registry_mod
    import api.repo_facts as repo_facts_mod
    monkeypatch.setattr(registry_mod, "find", lambda *a, **k: None)
    monkeypatch.setattr(repo_facts_mod, "search_packages", lambda *a, **k: "")

    body = client.get(ASK, params={"name": "notathing"}).json()

    assert body["buildable"] is False
    assert "certified blueprint" in body["explanation"]
    assert "container image" in body["explanation"]
    assert "no machine was spent" in body["reason"]
