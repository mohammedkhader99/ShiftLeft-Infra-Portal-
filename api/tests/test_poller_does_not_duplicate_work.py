"""The poller must not start the same work twice, or look dead while working.

REQ-2026-0161 (decommission of REQ-2026-0153) exposed two faults that are really
one: a sweep runs INLINE and blocks on real infrastructure work far longer than
anything around it expects.

  1. _decommission left the request at 'submitted' for the whole of a call that
     can block for ORCHESTRATOR_TIMEOUT_SECONDS (3000). The sweep collects
     'submitted', so every 30 seconds it handed off ANOTHER destroy of the same
     cluster. The audit trail shows destroy.handoff at 20:40:33 and again at
     20:48:20, and the second Terraform run collided with the first on the state
     lock. The provisioning path had always claimed its request first — the
     asymmetry was the bug.

  2. The leader lease (60s TTL) is renewed at the top of the poll loop, which a
     blocked sweep never reaches. The lease lapsed at 20:41:24 while the poller
     was working perfectly well, so it looked dead — and restarting the API was
     what started the duplicate destroy.

These tests drive the real functions. A shape assertion would have passed
against the broken code in both cases.
"""

import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from db.models import (Approval, Blueprint, ProvisionedResource, Request,
                       RequestComponent)
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
    """A session PLUS the engine, so a test can open a SECOND session and see
    only what has actually been committed — which is the whole question here."""
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = Factory()
    seed(s)
    s.add(Blueprint(technology_code="nginx", deployment_target="oci",
                    blueprint_ref="oci/service-vm", status="certified",
                    resource_kind="oci-service-vm"))
    source = Request(reference="REQ-2026-0153", requester="a@b.com",
                     request_type="create", status="provisioned",
                     deployment_target="oci", environment_name="test")
    source.components = [RequestComponent(technology_code="nginx", size="small")]
    s.add(source)
    s.flush()
    s.add(Approval(request_id=source.id, jira_key="SDIMD-79624", status="Approved"))
    s.add(ProvisionedResource(reference="REQ-2026-0153", kind="oci-service-vm",
                              name="test-req-2026-0153-service-vm",
                              details={}, lifecycle_state="active"))
    s.commit()
    yield s, Factory
    s.close()


def _ok_response():
    """The shape _decommission expects back from the orchestrator."""
    return type("R", (), {"status_code": 200, "text": "{}",
                          "json": lambda self=None: {}})()


def _decommission_request(session, reference="REQ-2026-0161") -> Request:
    req = Request(reference=reference, requester="a@b.com",
                  request_type="decommission", status="submitted",
                  source_reference="REQ-2026-0153", deployment_target="oci")
    req.components = [RequestComponent(technology_code="nginx", size="small")]
    session.add(req)
    session.flush()
    # jira_key is unique, so each request needs its own — the source already
    # holds SDIMD-79624.
    session.add(Approval(request_id=req.id, jira_key=f"SDIMD-{reference[-4:]}",
                         status="Approved"))
    session.commit()
    return req


# --- 1. The request is claimed before the long call --------------------------

def test_the_request_is_claimed_before_the_destroy_is_handed_off(db, monkeypatch):
    """THE bug. While the destroy is in flight the request must not still read
    'submitted' to anyone else, because the sweep collects 'submitted'.

    Checked from a SEPARATE session, so it proves the claim was COMMITTED before
    the blocking call — not merely assigned on an object the sweep cannot see.
    """
    session, Factory = db
    req = _decommission_request(session)

    seen = {}

    def fake_post(body, signature, path="/provision"):
        # Exactly where the real code blocks for up to 3000 seconds.
        with Factory() as other:
            row = other.query(Request).filter_by(reference="REQ-2026-0161").one()
            seen["status_during_call"] = row.status
        return _ok_response(), None

    monkeypatch.setattr(main, "_post_to_orchestrator", fake_post)
    monkeypatch.setattr(main, "_transition_jira", lambda *a, **k: None)
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)

    main._decommission(session, req, actor="poller")

    assert seen["status_during_call"] != "submitted", (
        "the request was still 'submitted' while its destroy was running — the "
        "next sweep would start a second destroy of the same infrastructure")
    assert seen["status_during_call"] == "decommissioning", seen


def test_the_sweep_will_not_collect_a_request_mid_teardown(db, monkeypatch):
    """The claim is only worth anything if the sweep actually skips it.

    Drives _poll_once with two requests: one mid-teardown, one genuinely waiting.
    The sweep must advance the second and leave the first alone.
    """
    session, Factory = db
    mid = _decommission_request(session, "REQ-2026-0161")
    mid.status = "decommissioning"
    waiting = _decommission_request(session, "REQ-2026-0162")
    session.commit()

    advanced = []
    monkeypatch.setattr(main, "SessionLocal", Factory)
    monkeypatch.setattr(main, "_advance_request",
                        lambda s, r: advanced.append(r.reference))
    monkeypatch.setattr(main, "_escalate_sla", lambda s, r: None)
    for sweep in ("_sweep_ttls", "_sweep_project_expiry"):
        if hasattr(main, sweep):
            monkeypatch.setattr(main, sweep, lambda *a, **k: None)

    main._poll_once()

    assert "REQ-2026-0161" not in advanced, (
        "a teardown already in flight was picked up again")
    assert "REQ-2026-0162" in advanced, (
        "the sweep stopped collecting work it should still do")


def test_decommissioning_is_a_terminalish_claim_not_a_silent_stall(db, monkeypatch):
    """A claimed request must still reach a real end state when the call returns.

    Claiming without releasing would trade a duplicate-work bug for a stuck-
    forever bug, which is not an improvement.
    """
    session, Factory = db
    req = _decommission_request(session)
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (_ok_response(), None))
    monkeypatch.setattr(main, "_transition_jira", lambda *a, **k: None)
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)

    main._decommission(session, req, actor="poller")
    assert req.status == "decommissioned", req.status

    failed = _decommission_request(session, "REQ-2026-0163")
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (None, "unreachable: boom"))
    main._decommission(session, failed, actor="poller")
    assert failed.status == "teardown-failed", failed.status


# --- 2. The lease survives a long sweep, but not a wedged one ----------------

def test_the_lease_is_renewed_while_a_sweep_is_still_working(monkeypatch):
    """A sweep that blocks for an hour must not look like a dead poller.

    The loop cannot renew while _poll_once blocks, so renewal runs beside it.
    """
    renewals = []
    monkeypatch.setattr(main.leader, "try_acquire",
                        lambda session, name="poller": renewals.append(name) or True)
    monkeypatch.setattr(main, "_audit_leader", lambda event: None)

    stop = threading.Event()
    worker = threading.Thread(
        target=main._renew_lease_during_sweep,
        args=(stop, 0.02, time.monotonic() + 30), daemon=True)
    worker.start()
    time.sleep(0.2)          # a "long" sweep, in miniature
    stop.set()
    worker.join(timeout=2)

    assert len(renewals) >= 2, (
        f"the lease was renewed {len(renewals)} times during a sweep; it would "
        f"have lapsed while the poller was working")


def test_the_watchdog_stops_renewing_a_wedged_sweep(monkeypatch):
    """The escape hatch. Renewing forever would let a hung loop hold the lease
    and block every standby — trading a false death for a real one."""
    renewals, audited = [], []
    monkeypatch.setattr(main.leader, "try_acquire",
                        lambda session, name="poller": renewals.append(name) or True)
    monkeypatch.setattr(main, "_audit_leader", lambda event: audited.append(event))

    stop = threading.Event()
    # Deadline already passed: this sweep is considered wedged.
    main._renew_lease_during_sweep(stop, 0.01, time.monotonic() - 1)

    assert renewals == [], "a wedged sweep kept renewing the lease"
    assert audited == ["poller.sweep.watchdog"], audited


def test_the_watchdog_window_clears_a_real_terraform_apply():
    """The deadline must sit above the longest LEGITIMATE sweep, or a slow build
    is mistaken for a hang — the same mistake in the other direction."""
    assert main.sweep_watchdog_seconds() > main.ORCH_TIMEOUT, (
        "the watchdog would fire during a normal apply")


def test_the_watchdog_is_configurable_and_floored(monkeypatch):
    monkeypatch.setenv("POLLER_SWEEP_WATCHDOG_SECONDS", "1200")
    assert main.sweep_watchdog_seconds() == 1200
    monkeypatch.setenv("POLLER_SWEEP_WATCHDOG_SECONDS", "not-a-number")
    assert main.sweep_watchdog_seconds() > 0, "a bad value must not disable the loop"


def test_the_poll_loop_really_renews_while_its_sweep_blocks(monkeypatch):
    """The integration point, not the helper.

    Testing _renew_lease_during_sweep alone proves nothing about _poller_loop:
    delete the heartbeat thread and that test still passes. This drives the loop
    with a sweep that BLOCKS, and asserts the lease was renewed DURING the block
    — which is the only thing that was actually broken.
    """
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "2")
    monkeypatch.setenv("LEADER_LEASE_TTL_SECONDS", "5")
    monkeypatch.setattr(main, "_audit_leader", lambda event: None)

    renewals: list[float] = []
    monkeypatch.setattr(main.leader, "try_acquire",
                        lambda session, name="poller": (renewals.append(time.monotonic())
                                                        or True))

    class _NullSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(main, "SessionLocal", _NullSession)

    sweep = {}

    def slow_sweep():
        sweep["start"] = time.monotonic()
        # Must outlive the renew interval, which has a 2s floor
        # (renew = max(2, min(interval, ttl // 2))). The heartbeat waits one
        # interval before its FIRST renewal, which is safe in production —
        # renew can never exceed half the lease TTL — but it means a sub-2s
        # sweep here would prove nothing.
        time.sleep(2.6)
        sweep["end"] = time.monotonic()

    monkeypatch.setattr(main, "_poll_once", slow_sweep)

    main._poller_stop.clear()
    loop = threading.Thread(target=main._poller_loop, daemon=True)
    loop.start()
    time.sleep(3.2)
    main._poller_stop.set()
    loop.join(timeout=3)

    assert "end" in sweep, "the sweep never ran"
    during = [t for t in renewals if sweep["start"] < t < sweep["end"]]
    assert during, (
        "the lease was never renewed while the sweep was blocked — this is the "
        "REQ-2026-0161 failure: a working poller whose lease lapsed at 20:41:24")


# --- 3. Nothing the sweep calls may block forever ----------------------------

def test_every_outbound_call_the_sweep_makes_has_a_timeout():
    """A call with no timeout can block the poll loop indefinitely.

    The lease watchdog bounds the damage, but only after the deadline: an untimed
    socket read can hold the sweep for as long as the peer keeps the connection
    open. Jira proved twice on 2026-08-17 that this network is not reliable — one
    read timeout, one DNS failure — so the timeout is what keeps a bad afternoon
    from becoming a stalled poller.

    Every call is timed today. This exists so the next one is too.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for name in ("api/jira.py", "api/main.py"):
        path = root / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            if getattr(fn.value, "id", "") != "httpx":
                continue
            if fn.attr in ("Client", "AsyncClient", "Timeout"):
                continue
            if not any(k.arg == "timeout" for k in node.keywords):
                offenders.append(f"{name}:{node.lineno} httpx.{fn.attr}()")

    assert not offenders, (
        "these outbound calls have no timeout and can stall the poller:\n  "
        + "\n  ".join(offenders))
