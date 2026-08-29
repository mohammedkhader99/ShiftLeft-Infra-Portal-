"""The golden-image lifecycle never ran once, for four days.

`_poll_once` uses `proof_wiring`, which is NOT imported at module level in
api/main.py — every other call site in that file imports it locally, and the
golden block added with G2 on 2026-08-25 did not. So it raised NameError on
EVERY poll, roughly every forty seconds, from the day it shipped:

    golden.sweep.error  {'error': "name 'proof_wiring' is not defined"}

promote, supersede, expire and reap never executed once. Captured images were
never made usable, never retired and never deleted — the entire second half of
"keep the machine a proof proved, reuse it, retire it, delete it".

IT WAS INVISIBLE BECAUSE THE GUARD WORKED AS DESIGNED. The block has its own
try/except on purpose, so an unreachable orchestrator cannot take the
certification sweep down with it. From outside, a NameError looks exactly like
an unreachable orchestrator. The only trace was an audit line nothing read.

So these tests do not assert that an import statement exists — that is the
mistake this file exists to correct, an assertion satisfied by the text rather
than by the behaviour. They assert the block RUNS: that the lifecycle
collaborators are actually reached, and that no sweep error is recorded.

Nothing here reaches an orchestrator or a cloud.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import AuditLog
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = Factory()
    seed(s)
    s.commit()
    s.factory = Factory
    yield s
    s.close()


@pytest.fixture()
def swept(db, monkeypatch):
    """Drive one real poll, recording which lifecycle steps were reached."""
    from api import golden, main

    monkeypatch.setattr(main, "SessionLocal", db.factory)
    monkeypatch.setattr(main, "_advance_request", lambda s, r: None)
    monkeypatch.setattr(main, "_escalate_sla", lambda s, r: None)
    for other in ("_sweep_ttls", "_sweep_project_expiry", "_sweep_reports"):
        if hasattr(main, other):
            monkeypatch.setattr(main, other, lambda *a, **k: None)
    # The orchestrator is never called: every collaborator that would talk to it
    # is replaced, so what is left under test is whether the block executes.
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (None, "not in a test"))

    reached: list[str] = []
    monkeypatch.setattr(golden, "promote",
                        lambda s, states: reached.append("promote") or [])
    monkeypatch.setattr(golden, "supersede",
                        lambda s: reached.append("supersede") or [])
    monkeypatch.setattr(golden, "expire",
                        lambda s: reached.append("expire") or [])
    monkeypatch.setattr(golden, "reap",
                        lambda s, delete: reached.append("reap") or [])

    main._poll_once()
    return reached, db


def _errors(session):
    return [a for a in session.scalars(select(AuditLog)).all()
            if a.event == "golden.sweep.error"]


# --- the block runs at all -----------------------------------------------------

def test_the_golden_sweep_reaches_its_first_step(swept):
    """THE test. For four days this raised before the first call."""
    reached, _ = swept

    assert "promote" in reached, (
        "the golden sweep never reached promote — it is still failing before "
        f"its first collaborator; reached: {reached}")


def test_every_step_of_the_lifecycle_is_reached(swept):
    """Retiring and deleting are the half that never ran. An image that is
    promoted but never reaped is a bill that never stops."""
    reached, _ = swept

    assert reached == ["promote", "supersede", "expire", "reap"], reached


def test_the_order_puts_taking_out_of_service_before_deleting(swept):
    """A single cycle must never delete an image it has not first taken out of
    service — the rule the block's own comment states."""
    reached, _ = swept

    assert reached.index("reap") == len(reached) - 1, reached
    for stopped in ("supersede", "expire"):
        assert reached.index(stopped) < reached.index("reap"), reached


def test_no_sweep_error_is_recorded(swept):
    """The audit line that was the only trace of this, every forty seconds."""
    _, session = swept

    assert _errors(session) == [], [a.detail for a in _errors(session)]


# --- and the guard that hid it still guards -----------------------------------

def test_a_failing_lifecycle_step_is_still_caught_and_audited(db, monkeypatch):
    """The try/except is not the defect and must not be removed. An unreachable
    orchestrator still has to be survivable — it simply must no longer be
    indistinguishable from a NameError, which is what the tests above ensure."""
    from api import golden, main

    monkeypatch.setattr(main, "SessionLocal", db.factory)
    monkeypatch.setattr(main, "_advance_request", lambda s, r: None)
    monkeypatch.setattr(main, "_escalate_sla", lambda s, r: None)
    for other in ("_sweep_ttls", "_sweep_project_expiry", "_sweep_reports"):
        if hasattr(main, other):
            monkeypatch.setattr(main, other, lambda *a, **k: None)
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (None, "not in a test"))

    def unreachable(*a, **k):
        raise RuntimeError("orchestrator unreachable")

    monkeypatch.setattr(golden, "promote", unreachable)

    main._poll_once()  # must not raise

    errors = _errors(db)
    assert errors, "a failing sweep was neither survived nor recorded"
    assert "unreachable" in str(errors[0].detail)
