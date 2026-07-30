"""F-SEC-01: the append-only audit chain is tamper-evident + verifiable."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.audit import append_audit, verify_chain
from db.models import AuditLog
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


def _seed(session, n=4):
    for i in range(n):
        append_audit(session, f"event.{i}", reference="REQ-1", actor="alice", detail={"i": i})
    session.commit()


def _rows(session):
    return session.scalars(select(AuditLog).order_by(AuditLog.id)).all()


def test_intact_chain_verifies(session):
    _seed(session)
    result = verify_chain(session)
    assert result["ok"] is True
    assert result["checked"] == 4
    assert result["total"] == 4
    assert result["broken_at"] is None


def test_edited_entry_is_detected(session):
    _seed(session)
    row = _rows(session)[1]
    row.detail = {"i": 999}  # tamper the content without recomputing the hash
    session.commit()
    result = verify_chain(session)
    assert result["ok"] is False
    assert result["broken_at"]["id"] == row.id
    assert "hash mismatch" in result["broken_at"]["reason"]


def test_deleted_entry_is_detected(session):
    _seed(session)
    session.delete(_rows(session)[1])  # remove a middle entry -> its child now dangles
    session.commit()
    result = verify_chain(session)
    assert result["ok"] is False
    assert "missing" in result["broken_at"]["reason"]


def test_concurrent_fork_still_verifies(session):
    # Two entries chained to the SAME parent (a concurrency fork) is not
    # tampering — the log must still verify intact.
    from datetime import datetime, timezone

    from api.audit import _content, _entry_hash

    append_audit(session, "root", detail={"i": 0})
    append_audit(session, "child.a", detail={"i": 1})  # normal, chains to root
    session.commit()
    root = _rows(session)[0]
    # A second child chained to root, as a racing writer would produce.
    now = datetime.now(timezone.utc)
    content = _content("child.b", None, None, None, {"i": 2}, now)
    session.add(AuditLog(event="child.b", detail={"i": 2}, prev_hash=root.entry_hash,
                         entry_hash=_entry_hash(root.entry_hash, content), created_at=now))
    session.commit()
    assert verify_chain(session)["ok"] is True


def test_empty_log_is_ok(session):
    assert verify_chain(session) == {"ok": True, "checked": 0, "total": 0, "broken_at": None}


def test_hmac_signing_detects_a_wrong_key(session, monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_KEY", "s3cret-key")
    _seed(session)
    assert verify_chain(session)["ok"] is True          # right key verifies
    monkeypatch.setenv("AUDIT_HMAC_KEY", "wrong-key")
    assert verify_chain(session)["ok"] is False          # forged/altered key fails
