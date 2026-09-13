"""The IaC scan of a saved plan has to have run before "no findings" means it.

Found on 2026-09-13 while reading `provisioner.py` for something else.
`_scan_saved_plan` read `tmo`, which is a local of `terraform_plan` and was never
in scope there. Every call raised NameError. The blanket `except Exception` below
it caught that and returned the empty result — `{"high": 0, "ok": True}` — and
the gate in `/provision` read the zero and let the plan through.

So F-SEC-03/04 reported a clean scan on every plan from 2026-08-06 to
2026-09-13, including ones carrying `classification: confidential`, and it did so
with no symptom whatsoever: a clean scan and a scan that never ran are the same
two words on the screen.

IT WAS CORRECT WHEN IT WAS WRITTEN, a week earlier. b92f63e added per-blueprint
timeouts — OCI takes 10-20 minutes to build a cluster and a 600-second kill part
way through an apply is the worst outcome available — and rewrote all fifteen
`_run` calls in the file to pass `timeout=tmo`. Fourteen were inside functions
that had just computed it. A uniform edit is the change that does not stop to ask
whether the sites are uniform.

Confirmed against REQ-2026-0315's own saved plan before the fix:

    {'findings': [], 'counts': {...}, 'high': 0, 'ok': True,
     'error': "name 'tmo' is not defined"}

WHAT MADE IT INVISIBLE IS A DEFENSIVE PATTERN DOING ITS JOB. "Never breaks the
plan" is the right call — a scanner hiccup should not stop a legitimate build —
and a fallback that reports SUCCESS is indistinguishable from success. The
result has always carried an `error` key for exactly this; nothing read it.

So these tests read it.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import provisioner

# One resource, one HIGH finding: a bucket anyone on the internet can read. The
# point is not the rule — test_scanner.py covers the rules — it is that a
# document with something in it does not come back empty.
PUBLIC_BUCKET = {
    "resource_changes": [{
        "type": "oci_objectstorage_bucket",
        "name": "evidence",
        "change": {"actions": ["create"],
                   "after": {"access_type": "ObjectRead", "name": "evidence"}},
    }]
}


class Show:
    """What `terraform show -json` looks like when it worked."""
    returncode = 0
    stderr = ""

    def __init__(self, document):
        self.stdout = json.dumps(document)


@pytest.fixture()
def ran(monkeypatch):
    """Record every terraform invocation the scan makes."""
    calls: list[dict] = []

    def record(args, workdir, timeout=0):
        calls.append({"args": args, "timeout": timeout})
        return Show(PUBLIC_BUCKET)

    monkeypatch.setattr(provisioner, "_run", record)
    return calls


# --- it ran -------------------------------------------------------------------

def test_the_scanner_actually_ran(tmp_path, ran):
    """THE DEFECT, stated as the only assertion that would have caught it. Not
    "are there findings" — there were none to find, truthfully, for five weeks —
    but "did anything look"."""
    result = provisioner._scan_saved_plan(tmp_path, "internal")

    assert "error" not in result, result["error"]
    assert ran, "terraform show was never called"


def test_it_reads_the_saved_plan(tmp_path, ran):
    provisioner._scan_saved_plan(tmp_path, "internal")

    assert ran[0]["args"] == ["show", "-json", provisioner.PLAN_FILE]


def test_findings_in_the_plan_reach_the_result(tmp_path, ran):
    """The consequence of the NameError, from the other end: a plan with a HIGH
    finding in it scored zero."""
    result = provisioner._scan_saved_plan(tmp_path, "internal")

    assert result["high"] >= 1, result
    assert result["findings"]


def test_a_blueprint_that_takes_twenty_minutes_gets_twenty_minutes(tmp_path, ran):
    """The timeout is the argument that was missing. A cluster's manifest asks
    for longer than the default, and the scan of its plan runs under the same
    clock as the plan itself."""
    provisioner._scan_saved_plan(tmp_path, "internal", 1800)

    assert ran[0]["timeout"] == 1800


def test_the_default_timeout_is_the_ordinary_one(tmp_path, ran):
    provisioner._scan_saved_plan(tmp_path, "internal")

    assert ran[0]["timeout"] == provisioner.DEFAULT_COMMAND_TIMEOUT


# --- and it still never breaks a plan -----------------------------------------

def test_a_failing_show_is_reported_not_raised(tmp_path, monkeypatch):
    """The behaviour the blanket catch was written for, which is still right: a
    scanner that cannot read the plan must not stop a legitimate build."""
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "no plan file"

    monkeypatch.setattr(provisioner, "_run", lambda *a, **k: Failed())
    result = provisioner._scan_saved_plan(tmp_path, "internal")

    assert result["ok"] is True and result["high"] == 0
    assert result["error"] == "terraform show failed"


def test_an_unexpected_failure_is_reported_not_raised(tmp_path, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("terraform is not installed")

    monkeypatch.setattr(provisioner, "_run", explode)
    result = provisioner._scan_saved_plan(tmp_path, "internal")

    assert result["ok"] is True
    assert "not installed" in result["error"]


def test_every_swallowed_failure_says_so(tmp_path, monkeypatch):
    """THE PROPERTY THAT MATTERS MORE THAN ANY SINGLE FIX. A fallback result is
    allowed; a fallback result that cannot be told apart from a real one is not.
    Whatever goes wrong in here, the caller can find out that it did."""
    for breakage in (RuntimeError("boom"), ValueError("not json")):
        monkeypatch.setattr(provisioner, "_run",
                            lambda *a, _b=breakage, **k: (_ for _ in ()).throw(_b))
        assert provisioner._scan_saved_plan(tmp_path, "internal").get("error")
