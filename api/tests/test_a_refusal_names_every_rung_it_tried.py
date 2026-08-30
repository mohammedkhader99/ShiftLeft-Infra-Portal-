"""A refusal that names one rung leaves the reader to guess about the rest.

REQ-2026-0236 asked for OpenSearch and was told:

    "no container image was found on a registry this portal trusts"

True, and it told nobody where to look. The image was on Docker Hub the whole
time, under an account the trust rule happened not to match. Nothing in that
sentence would have led anyone to the real problem — and the reader could not
tell whether OCI had been asked, whether a package existed, or whether a recipe
was one proof away.

So a refusal now names EVERY way this portal knows to deliver software, and what
each one answered.

TWO PROPERTIES CARRY IT.

  * It reports what was ACTUALLY ASKED. The answers are recorded as the ladder
    consults each source, never re-derived afterwards: a second lookup can
    answer differently and would describe a run that never happened.
  * It says which rungs DO NOT EXIST YET rather than omitting them. "Not
    checked, because no registry credential is configured" is a different
    statement from "checked and found nothing", and only one of them tells the
    reader what to do next.

Nothing here reaches the network or a machine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import autobuild
from db.seed import seed
from db.session import Base


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("AUTOBUILD_ENABLED", "AUTOBUILD_MAX_ATTEMPTS", "AI_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AUTOBUILD_ENABLED", "true")
    monkeypatch.setenv("AI_MODE", "mock")


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.commit()
    yield s
    s.close()


def refusal(db, *, packages=(), image=None, code="opensearch"):
    result = autobuild.ensure(
        code, db, target="oci", shipped=lambda c: None,
        run_proof=lambda s, m: None, publish=lambda f: list(f),
        certify=lambda m, r: None, withdraw=lambda f: list(f),
        search=lambda c, family: list(packages), find_image=lambda c: image)
    assert result.status == "refused", result.status
    return result.detail


# --- every rung is named ---------------------------------------------------------

@pytest.mark.parametrize("rung", [
    "certified blueprint",
    "reviewed recipe",
    "OS packages",
    "vendor repository",
    "archive",
    "container image",
    "OCI managed service",
    "OCI container registry",
])
def test_every_rung_appears_in_the_refusal(db, rung):
    assert rung in refusal(db), rung


def test_the_rungs_that_do_not_exist_yet_say_so(db):
    """Omitting them would read as "checked and found nothing", which is the
    opposite of true and hides the two things that would most help."""
    detail = refusal(db)

    assert "not checked" in detail
    assert "no registry credential is configured" in detail
    assert "cannot yet ask OCI" in detail


def test_it_still_says_no_machine_was_spent(db):
    assert "no machine was spent" in refusal(db)


def test_it_still_guides(db):
    """A refusal must explain and guide, never just say no."""
    assert "allow-listed registry" in refusal(db)


# --- and it reports what was really asked ---------------------------------------

def test_a_package_that_was_found_is_named():
    """If the repositories DID offer something, the reader must see it — that is
    a different situation from nothing being packaged, even when the run still
    ends in a refusal for another reason."""
    line = autobuild._why_nothing_works(
        "x", {"packages": ["opensearch-oss"], "image": None},
        curated_repo=False, curated_archive=False)

    assert "opensearch-oss" in line


def test_an_image_that_was_found_is_named():
    line = autobuild._why_nothing_works(
        "x", {"packages": [], "image": {"image": "docker.io/library/x"}},
        curated_repo=False, curated_archive=False)

    assert "docker.io/library/x" in line


def test_a_source_that_was_never_asked_is_not_reported_as_empty():
    """THE honesty property. A collaborator that was not supplied has said
    nothing — reporting that as "nothing found" would be inventing evidence."""
    line = autobuild._why_nothing_works("x", {}, curated_repo=False,
                                        curated_archive=False)

    # ASSERTED PER RUNG. Checking for "not searched" anywhere in the block was
    # satisfied by the container line while the package line said something
    # else entirely — a plant against the package rung broke nothing.
    packages_line = next(r for r in line.splitlines() if "OS packages" in r)
    image_line = next(r for r in line.splitlines() if "container image" in r)

    assert "not searched" in packages_line, packages_line
    assert "not searched" in image_line, image_line
    assert "nothing named it" not in packages_line


def test_a_curated_repository_is_reported_as_tried(db):
    """Vault has a curated vendor repository. If one exists and the technology
    still could not be built, "none curated for it" would be a lie."""
    line = autobuild._why_nothing_works("vault", {"packages": [], "image": None},
                                        curated_repo=True, curated_archive=False)

    assert "curated, and it did not work" in line
    assert "none curated for it" in line  # the archive line, which is true


def test_the_answers_come_from_the_run_not_a_second_lookup(db):
    """Recorded as the ladder consults each source. A second lookup could answer
    differently and would describe a run that never happened."""
    calls: list = []

    autobuild.ensure(
        "opensearch", db, target="oci", shipped=lambda c: None,
        run_proof=lambda s, m: None, publish=lambda f: list(f),
        certify=lambda m, r: None, withdraw=lambda f: list(f),
        search=lambda c, family: calls.append("search") or [],
        find_image=lambda c: calls.append("image") or None)

    assert calls.count("search") == 1, calls
    assert calls.count("image") <= 1, calls
