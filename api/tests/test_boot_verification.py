"""A request is provisioned when the MACHINE says so, not when Terraform exits 0.

WHY THIS FILE EXISTS. Terraform returning success means an instance exists. It
says nothing about whether the software the requester asked for is on it. The
portal treated the two as the same thing, and was wrong four times in one week —
a service-vm that installed nothing, an Apache whose runcmd was discarded, a
Redis 6 sold as a 7, an nginx that lost port 80. Every one booted healthy and was
recorded `provisioned`.

REQ-2026-0134 was the last of them: nginx on Ubuntu could not reach the apt
repositories, the machine came up empty, and the portal reported success. Its own
self-report said `nginx NOT INSTALLED` the whole time — nothing read it.

So these tests are about the reading. The three outcomes are deliberately
distinct because they need different actions from a human:

    ok       the machine confirms what is installed and running
    broken   the machine says what is wrong, in its own words
    silent   nothing reported, so the portal does not KNOW — which is NOT
             permission to assume health. That assumption is the whole bug.
"""

from __future__ import annotations

import pytest

from api import main as api_main


@pytest.fixture(autouse=True)
def _fast_and_enforced(monkeypatch):
    """Verification on, with a clock the test drives instead of waiting on.

    Sleeping for real would make these tests take minutes. Patching sleep to do
    NOTHING is worse than that: the loop then spins against a real clock, which
    is how the first run of this file span millions of times in a minute and
    turned a genuine race in the deadline check into a coin flip. So sleeping
    advances a fake clock by exactly the time it claims to.
    """
    monkeypatch.setenv("BOOT_VERIFY_ENFORCED", "true")
    monkeypatch.setenv("BOOT_VERIFY_DEADLINE_MINUTES", "1")
    monkeypatch.setenv("BOOT_VERIFY_POLL_SECONDS", "5")
    clock = {"now": 1000.0}
    monkeypatch.setattr(api_main.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(api_main.time, "sleep",
                        lambda seconds: clock.__setitem__("now", clock["now"] + seconds))


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def _orchestrator_says(monkeypatch, *replies):
    """Answer /verify with each reply in turn, repeating the last one forever."""
    calls = {"n": 0}

    def fake_post(body, signature, path="/provision"):
        assert path == "/verify"
        index = min(calls["n"], len(replies) - 1)
        calls["n"] += 1
        reply = replies[index]
        return (reply, None) if isinstance(reply, _Response) else (None, str(reply))

    monkeypatch.setattr(api_main, "_post_to_orchestrator", fake_post)
    return calls


HEALTHY = _Response({"checked": 1, "settled": True, "all_ok": True, "waiting": [],
                     "broken": [], "resources": [{"kind": "oci-service-vm",
                                                  "expected": True, "state": "ok"}]})
BROKEN = _Response({"checked": 1, "settled": True, "all_ok": False, "waiting": [],
                    "broken": ["oci-service-vm"],
                    "resources": [{"kind": "oci-service-vm", "expected": True,
                                   "state": "broken",
                                   "problems": ["nginx NOT INSTALLED",
                                                "nginx is inactive"]}]})
WAITING = _Response({"checked": 1, "settled": False, "all_ok": False,
                     "waiting": ["oci-service-vm"], "broken": [],
                     "resources": [{"kind": "oci-service-vm", "expected": True,
                                    "state": "waiting"}]})
NOTHING_TO_CHECK = _Response({"checked": 0, "settled": True, "all_ok": True,
                              "waiting": [], "broken": [], "resources": []})


def _verify(monkeypatch, *replies):
    _orchestrator_says(monkeypatch, *replies)
    return api_main._await_boot_reports("REQ-2026-0199", b"{}", "sig")


# --- The three outcomes -------------------------------------------------------

def test_a_machine_that_confirms_itself_passes(monkeypatch):
    outcome = _verify(monkeypatch, HEALTHY)
    assert outcome["ok"] is True
    assert outcome["event"] == "verification.passed"


def test_a_machine_that_says_it_is_broken_fails_the_request(monkeypatch):
    """THE test. REQ-2026-0134 said exactly this and was called provisioned."""
    outcome = _verify(monkeypatch, BROKEN)
    assert outcome["ok"] is False
    assert outcome["event"] == "verification.failed"
    assert "nginx NOT INSTALLED" in outcome["summary"]


def test_the_machines_own_words_reach_the_summary(monkeypatch):
    """A human reading 'failed' needs to know WHY without going to the bucket."""
    outcome = _verify(monkeypatch, BROKEN)
    for problem in ("nginx NOT INSTALLED", "nginx is inactive"):
        assert problem in outcome["summary"]


def test_silence_is_not_success(monkeypatch):
    """The inference this whole feature exists to kill: no news is good news."""
    outcome = _verify(monkeypatch, WAITING)
    assert outcome["ok"] is False
    assert outcome["event"] == "verification.silent"
    assert "NOT been confirmed working" in outcome["summary"]


def test_silence_and_brokenness_are_reported_differently(monkeypatch):
    """They need different actions — fix the machine, or fix the reporting path
    — so collapsing them into one 'failed' would send someone the wrong way."""
    assert (_verify(monkeypatch, WAITING)["event"]
            != _verify(monkeypatch, BROKEN)["event"])


# --- Waiting for a boot that is still going -----------------------------------

def test_it_waits_rather_than_calling_a_slow_boot_a_failure(monkeypatch):
    """Terraform returns when the instance is RUNNING, which is BEFORE cloud-init
    has installed anything. Asking once would fail every healthy machine."""
    calls = _orchestrator_says(monkeypatch, WAITING, WAITING, HEALTHY)
    outcome = api_main._await_boot_reports("REQ-2026-0199", b"{}", "sig")
    assert outcome["ok"] is True
    assert calls["n"] >= 3, "it gave up before the machine had finished booting"


def test_it_stops_waiting_eventually(monkeypatch):
    """Without a deadline a stuck machine holds the request open forever."""
    calls = _orchestrator_says(monkeypatch, WAITING)
    outcome = api_main._await_boot_reports("REQ-2026-0199", b"{}", "sig")
    assert outcome["ok"] is False
    # A 1-minute deadline polled every 5 seconds: about 13 attempts, not 13,000.
    assert calls["n"] <= 15, f"polled {calls['n']} times in one minute"


# --- Things that cannot report ------------------------------------------------

def test_a_request_with_nothing_to_check_passes(monkeypatch):
    """A bucket has no machine to disbelieve. Mock mode has no machine at all.
    Failing closed here would stop the demo path the project is built on."""
    outcome = _verify(monkeypatch, NOTHING_TO_CHECK)
    assert outcome["ok"] is True
    assert outcome["event"] == "verification.not_applicable"


def test_an_unreachable_orchestrator_does_not_pass_the_request(monkeypatch):
    """Not being able to ask is not the same as being told everything is fine."""
    outcome = _verify(monkeypatch, "connection refused")
    assert outcome["ok"] is False
    assert outcome["event"] == "verification.unavailable"


# --- The switch ---------------------------------------------------------------

def test_turning_enforcement_off_stops_it_blocking(monkeypatch):
    monkeypatch.setenv("BOOT_VERIFY_ENFORCED", "false")
    outcome = _verify(monkeypatch, BROKEN)
    assert outcome["ok"] is True
    assert outcome["event"] == "verification.skipped"


def test_it_is_on_unless_somebody_turns_it_off(monkeypatch):
    """A safety check that defaults to off is decoration."""
    monkeypatch.delenv("BOOT_VERIFY_ENFORCED", raising=False)
    assert api_main._boot_verify_enforced() is True


def test_a_nonsense_deadline_does_not_crash_provisioning(monkeypatch):
    monkeypatch.setenv("BOOT_VERIFY_DEADLINE_MINUTES", "not-a-number")
    assert api_main._boot_verify_int("BOOT_VERIFY_DEADLINE_MINUTES", 15) == 15


# --- The status it produces ---------------------------------------------------

def test_the_failure_status_fits_the_database_column():
    """varchar(16). 'verification-failed' is nineteen characters and would pass
    every SQLite test before failing on Postgres."""
    from db import models
    assert len("verify-failed") <= models.Request.__table__.columns["status"].type.length


def test_the_failure_status_is_not_apply_failed():
    """apply-failed means nothing was created and nothing needs cleaning up.
    Here the resources exist and are billing — saying apply-failed would send an
    operator looking for a Terraform error, and leave real machines running."""
    import inspect
    source = inspect.getsource(api_main._provision_in_background)
    assert 'req.status = "verify-failed"' in source
