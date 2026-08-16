"""A timeout is not a failure, and the API must outwait the orchestrator.

REQ-2026-0149 built a Kubernetes cluster with two worker nodes. The portal
recorded apply-failed and no resources at all, because the API's HTTP call timed
out at 5 minutes, was retried twice, and gave up at 15 — while the orchestrator
was still legitimately running `terraform apply`, which waits for the node pool.

The cluster and its VMs exist and are billing. Nothing in the portal knows.

Reporting failure falsely is WORSE than reporting success falsely. A false
success is at least recorded and can be torn down; a false failure loses live
infrastructure. This codebase spent a week on the first kind and met the second
the first time something took longer than five minutes.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from api import main as api_main

ROOT = pathlib.Path(__file__).resolve().parents[2]
BLUEPRINTS = ROOT / "orchestrator" / "blueprints"


def _longest_apply() -> tuple[str, int]:
    longest, name = 0, ""
    for path in BLUEPRINTS.glob("*.yaml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        seconds = int(doc.get("command_timeout_seconds") or 0)
        if seconds > longest:
            longest, name = seconds, path.name
    return name, longest


def test_the_api_outwaits_the_longest_apply():
    """THE mismatch. The API gave up at 5 minutes on work it allows 45."""
    name, seconds = _longest_apply()
    assert seconds, "no blueprint declares command_timeout_seconds"
    assert api_main.ORCH_TIMEOUT > seconds, (
        f"{name} may run for {seconds}s and the API waits only "
        f"{api_main.ORCH_TIMEOUT}s, so it will declare failure while the "
        f"orchestrator is still working")


def test_the_margin_is_not_razor_thin():
    """Equal values race: the orchestrator hits its own limit and returns an
    error the API should read, rather than both expiring together."""
    _name, seconds = _longest_apply()
    assert api_main.ORCH_TIMEOUT >= seconds + 120


def test_a_timeout_does_not_mark_the_request_failed():
    source = __import__("inspect").getsource(api_main._provision_in_background)
    assert "timed_out" in source
    marker = source.index("timed_out = ")
    failed = source.index('req.status = "apply-failed"')
    assert marker < failed, (
        "the timeout must be recognised BEFORE the failure branch, or a slow "
        "apply is still recorded as a failed one")


def test_a_timeout_is_audited_as_itself():
    """apply.failed and apply.timeout need different responses from a human: one
    is broken, the other may be a running build."""
    source = __import__("inspect").getsource(api_main._provision_in_background)
    assert '"apply.timeout"' in source


def test_a_real_refusal_is_still_a_failure():
    """Only a TIMEOUT is ambiguous. A 400 from the orchestrator means it refused,
    and treating that as 'maybe still building' would hide genuine faults."""
    source = __import__("inspect").getsource(api_main._provision_in_background)
    assert 'req.status = "apply-failed"' in source
    assert "elif response is None or response.status_code != 200:" in source


def test_the_timeout_is_configurable_without_a_code_change():
    """A blueprint slower than any today must not need an edit here."""
    source = pathlib.Path(api_main.__file__).read_text(encoding="utf-8")
    assert re.search(r'ORCH_TIMEOUT\s*=\s*float\(os\.getenv\(', source)


# --- What actually happens, not what the source looks like --------------------

def test_a_timed_out_apply_does_not_end_as_apply_failed(monkeypatch, tmp_path):
    """The behavioural test. Three source-shape assertions above all passed while
    `timed_out = False` was planted — they check that the code MENTIONS the case,
    not that it acts on it. This drives the function.
    """
    from db.models import Approval, Base, Request
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        s.add(Request(reference="REQ-T", status="in-progress", requester="u@x",
                      request_type="create", environment_name="env",
                      deployment_target="oci",
                      approval=Approval(jira_key="K-1", status="approved")))
        s.commit()

    monkeypatch.setattr(api_main, "SessionLocal", Session)
    monkeypatch.setattr(api_main, "_post_to_orchestrator",
                        lambda *a, **k: (None, "unreachable: timed out"))
    monkeypatch.setattr(api_main, "add_comment", lambda *a, **k: None)
    monkeypatch.setattr(api_main, "_transition_jira", lambda *a, **k: None)
    # Verification would poll a live orchestrator; the point here is the status.
    monkeypatch.setattr(api_main, "_await_boot_reports",
                        lambda *a, **k: {"ok": False, "event": "verification.silent",
                                         "summary": "still building", "detail": {}})

    api_main._provision_in_background("REQ-T", "K-1", b"{}", "sig", "")

    with Session() as s:
        req = s.scalar(select(Request).where(Request.reference == "REQ-T"))
        assert req.status != "apply-failed", (
            "a timed-out apply was recorded as failed — the cluster it may still "
            "be building would be lost, exactly as REQ-2026-0149's was")


def test_a_refused_apply_still_ends_as_apply_failed(monkeypatch):
    """The other half: only a TIMEOUT is ambiguous. A refusal must still fail, or
    genuine faults would sit in flight forever."""
    from db.models import Approval, Base, Request
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        s.add(Request(reference="REQ-R", status="in-progress", requester="u@x",
                      request_type="create", environment_name="env",
                      deployment_target="oci",
                      approval=Approval(jira_key="K-2", status="approved")))
        s.commit()

    class _Refused:
        status_code = 400
        text = "Terraform apply failed: invalid shape"

    monkeypatch.setattr(api_main, "SessionLocal", Session)
    monkeypatch.setattr(api_main, "_post_to_orchestrator",
                        lambda *a, **k: (_Refused(), None))
    monkeypatch.setattr(api_main, "add_comment", lambda *a, **k: None)

    api_main._provision_in_background("REQ-R", "K-2", b"{}", "sig", "")

    with Session() as s:
        assert s.scalar(select(Request).where(
            Request.reference == "REQ-R")).status == "apply-failed"
