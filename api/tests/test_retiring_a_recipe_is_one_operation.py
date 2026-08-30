"""Suspending a certification without removing its recipe does not hold.

On 2026-08-29 `oci-functions` was certified on a module that built nothing.
Suspending it by hand LOOKED like it worked — and `restore()` re-certified it
within one poll, because the generated blueprint MANIFEST was still in the
store, so the orchestrator went on answering "yes, I build oci-functions".

`restore()` was behaving exactly as designed: it only restores what the
orchestrator says it can build. The withdrawal was half done, and the only
reason anybody noticed was the sweep undoing it.

A recipe is more than one file. A profile is `<code>.json`; a drafted
blueprint's manifest is `<resource_kind>.yaml`. Removing one and not the other
leaves A MANIFEST WITH NO MODULE — worse than either alone, because it
advertises a recipe that would hand Terraform an empty directory.

So retirement is ONE operation that removes both, suspends the certification,
and then ASKS THE ORCHESTRATOR whether it took. A success that a sweep will
quietly undo is not a success.

Nothing here reaches an orchestrator or a cloud.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import certification
from db.models import Blueprint
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.add(Blueprint(technology_code="oci-functions", deployment_target="oci",
                    blueprint_ref="oci/oci-functions", status="certified",
                    resource_kind="oci-oci-functions"))
    s.commit()
    yield s
    s.close()


class Store:
    """The generated store. `deletes` records exactly what it was asked for."""

    def __init__(self, holds=()):
        self.holds = set(holds)
        self.asked: list = []

    def withdraw(self, files):
        self.asked.extend(sorted(files or {}))
        gone = [f for f in sorted(files or {}) if f in self.holds]
        self.holds -= set(gone)
        return gone


# --- both files, derived rather than guessed ------------------------------------

def test_the_manifest_is_removed_as_well_as_the_profile(db):
    """THE defect. Removing the module and leaving the manifest is what let the
    orchestrator go on claiming it."""
    store = Store(holds={"oci-functions.json", "oci-oci-functions.yaml"})

    out = certification.retire(db, "oci-functions", "oci", "why",
                               withdraw=store.withdraw, builds=set())

    assert "oci-oci-functions.yaml" in out.removed, out.removed
    assert "oci-functions.json" in out.removed, out.removed


def test_the_files_are_derived_from_the_row_not_from_the_caller(db):
    """A caller naming them by hand is how the manifest was left behind."""
    store = Store()

    certification.retire(db, "oci-functions", "oci", "why",
                         withdraw=store.withdraw, builds=set())

    assert store.asked == ["oci-functions.json", "oci-oci-functions.yaml"], store.asked


def test_the_certification_is_suspended(db):
    store = Store(holds={"oci-functions.json"})

    out = certification.retire(db, "oci-functions", "oci", "built nothing",
                               withdraw=store.withdraw, builds=set())

    assert out.suspended
    assert db.get(Blueprint, ("oci-functions", "oci")).status == certification.SUSPENDED


# --- and it says whether it actually took ---------------------------------------

def test_a_retirement_the_orchestrator_still_contradicts_is_not_complete(db):
    """THE test that would have caught the by-hand attempt. The orchestrator
    still claims it, so `restore()` will bring it back within one poll."""
    store = Store(holds={"oci-functions.json"})

    out = certification.retire(db, "oci-functions", "oci", "why",
                               withdraw=store.withdraw,
                               builds={"oci-functions"})

    assert out.complete is False
    assert "NOT RETIRED" in out.detail
    assert "restore" in out.detail or "sweep" in out.detail


def test_a_clean_retirement_says_so(db):
    store = Store(holds={"oci-functions.json", "oci-oci-functions.yaml"})

    out = certification.retire(db, "oci-functions", "oci", "why",
                               withdraw=store.withdraw, builds=set())

    assert out.complete is True
    assert "no longer builds it" in out.detail


def test_an_orchestrator_that_cannot_be_asked_is_not_a_success(db):
    """Absent is not the same as gone. The safe direction is to say so — a
    retirement believed and not real is how a certification comes back."""
    store = Store(holds={"oci-functions.json"})

    out = certification.retire(db, "oci-functions", "oci", "why",
                               withdraw=store.withdraw, builds=None)

    assert out.complete is False
    assert "could not be asked" in out.detail


def test_a_technology_with_no_blueprint_is_a_quiet_no_op(db):
    store = Store()

    out = certification.retire(db, "never-existed", "oci", "why",
                               withdraw=store.withdraw, builds=set())

    assert out.complete is True
    assert out.suspended is False
    assert store.asked == [], "it tried to delete files for a blueprint that never was"


def test_retiring_twice_is_not_an_error(db):
    """An operator who runs it again after a partial failure must not be
    punished for it."""
    store = Store(holds={"oci-functions.json", "oci-oci-functions.yaml"})

    first = certification.retire(db, "oci-functions", "oci", "why",
                                 withdraw=store.withdraw, builds=set())
    second = certification.retire(db, "oci-functions", "oci", "why",
                                  withdraw=store.withdraw, builds=set())

    assert first.complete and second.complete
    assert second.removed == [], second.removed
