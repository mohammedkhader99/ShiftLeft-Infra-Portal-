"""The real I/O behind a proof build and the autobuild loop.

`proof.run_proof` and `autobuild.build` take their collaborators as arguments so
the failure branches can be driven by tests. This is the other half: the real
implementations, and the two places where a translation layer can quietly change
a meaning.

Both of those places are here. Writing a draft outside the generated store would
let an agent overwrite a reviewed recipe; reading an unreadable verification as
healthy is the inference that reported four broken machines as provisioned.
"""

from __future__ import annotations

import json

import pytest

from api import proof_wiring


# --- publish: an agent may only write into its own store ---------------------

def test_a_yaml_draft_lands_in_blueprints_and_terraform_in_terraform(tmp_path):
    publish = proof_wiring.make_publish(tmp_path)
    written = publish({
        "orchestrator/blueprints/oci-cassandra.yaml": "ref: oci/cassandra\n",
        "orchestrator/terraform/oci/cassandra/main.tf": "resource \"x\" \"y\" {}\n",
    })

    assert (tmp_path / "blueprints" / "oci-cassandra.yaml").read_text() == "ref: oci/cassandra\n"
    assert (tmp_path / "terraform" / "main.tf").exists()
    assert len(written) == 2


def test_a_draft_cannot_write_outside_the_store(tmp_path):
    """THE property. `../../orchestrator/blueprints/oci-postgres.yaml` would
    overwrite a reviewed recipe — the same impersonation the registry refuses at
    discovery. Two doors into one room need two locks.
    """
    publish = proof_wiring.make_publish(tmp_path)
    publish({"../../../etc/passwd": "nope\n"})

    # Only the basename is ever used, so the traversal collapses to a file
    # INSIDE the store rather than escaping it.
    assert not (tmp_path.parent / "passwd").exists()
    assert (tmp_path / "terraform" / "passwd").exists()


def test_a_draft_cannot_overwrite_a_shipped_blueprint_by_naming_its_path(tmp_path):
    """Even with the shipped path spelled out in full, the write lands in the
    generated store — where discovery will then refuse it for shadowing."""
    publish = proof_wiring.make_publish(tmp_path)
    publish({"orchestrator/blueprints/oci-postgres.yaml": "ref: oci/postgres\n"})

    landed = tmp_path / "blueprints" / "oci-postgres.yaml"
    assert landed.exists()
    assert "orchestrator" not in str(landed.parent.parent.name)


def test_hidden_and_empty_names_are_skipped(tmp_path):
    publish = proof_wiring.make_publish(tmp_path)
    assert publish({".env": "SECRET=1\n", "": "x"}) == []
    assert not (tmp_path / "terraform" / ".env").exists()


# --- verify: silence is not health -------------------------------------------

def _post_returning(ok, detail):
    return lambda path, payload: (ok, detail)


def test_an_unreadable_verification_is_not_healthy():
    """No news is NOT good news. Reading an empty answer as healthy is the exact
    inference that reported four broken machines as provisioned."""
    verify = proof_wiring.make_verify(_post_returning(True, "not json at all"), deadline_seconds=0, sleep=lambda s: None)
    healthy, detail = verify("PROOF-X-20260821T090000", {"environment_tier": "Development"})
    assert healthy is False
    assert "could not be read" in detail


def test_a_broken_resource_is_reported_with_its_own_words():
    # The REAL response shape. These fixtures used to carry `resources` alone,
    # which /verify never sends — so they tested a reading of an answer that did
    # not exist.
    body = json.dumps({"checked": 1, "settled": True, "all_ok": False,
                       "broken": ["oci-oke"], "resources": [
                           {"kind": "oci-oke", "expected": True, "state": "broken",
                            "note": "node pool never reached ACTIVE"}]})
    verify = proof_wiring.make_verify(_post_returning(True, body), deadline_seconds=0, sleep=lambda s: None)
    healthy, detail = verify("PROOF-X-20260821T090000", {"environment_tier": "Development"})
    assert healthy is False
    assert "never reached ACTIVE" in detail


def test_still_waiting_is_not_healthy_either():
    """A machine that has not reported yet is unknown, and unknown is not a pass
    — the proof would otherwise tear it down and call it proven."""
    body = json.dumps({"checked": 1, "settled": False, "all_ok": False,
                       "waiting": ["oci-apache"], "resources": [
                           {"kind": "oci-apache", "expected": True, "state": "waiting"}]})
    verify = proof_wiring.make_verify(_post_returning(True, body), deadline_seconds=0, sleep=lambda s: None)
    assert verify("r", {"environment_tier": "Development"})[0] is False


def test_everything_healthy_passes():
    body = json.dumps({"checked": 1, "settled": True, "all_ok": True, "waiting": [],
                       "resources": [{"kind": "oci-apache", "expected": True,
                                      "state": "ok"}]})
    verify = proof_wiring.make_verify(_post_returning(True, body), deadline_seconds=0, sleep=lambda s: None)
    healthy, detail = verify("r", {"environment_tier": "Development"})
    assert healthy is True and "verified healthy" in detail


def test_an_unreachable_orchestrator_is_not_healthy():
    verify = proof_wiring.make_verify(_post_returning(False, "unreachable: timed out"), deadline_seconds=0, sleep=lambda s: None)
    healthy, detail = verify("r", {"environment_tier": "Development"})
    assert healthy is False
    assert "could not verify" in detail


# --- price: unpriceable is a refusal, not a crash ----------------------------

def test_an_unpriceable_plan_returns_none_rather_than_raising(monkeypatch):
    """check_cost refuses on None. A pricing failure must reach that decision
    rather than take the loop down with it."""
    def explode(*a, **k):
        raise RuntimeError("rate cards unavailable")
    monkeypatch.setattr(proof_wiring.pricing, "estimate_cost", explode)

    price = proof_wiring.make_price(None, "oci")
    assert price([{"technology_code": "x", "size": "small"}]) is None


def test_the_price_comes_from_the_portals_own_rate_cards(monkeypatch):
    """Never from the payload, and never from a model. The orchestrator prices it
    independently again before acting."""
    seen = {}

    def fake_estimate(components, target, session, advanced=None):
        seen["target"] = target
        return {"totals": {"monthly": 42.5}}
    monkeypatch.setattr(proof_wiring.pricing, "estimate_cost", fake_estimate)

    price = proof_wiring.make_price(None, "oci")
    assert price([{"technology_code": "x", "size": "small"}]) == 42.5
    assert seen["target"] == "oci"


# --- post: the orchestrator's refusal is the useful part ---------------------

def test_a_refusal_carries_the_reason_back():
    """The orchestrator names which bound was exceeded — the sandbox tier, the
    cost cap, a reference it did not mint. Swallowing that would leave the loop
    unable to say why it stopped."""
    class Resp:
        status_code = 409
        text = "Plan prices at 1,240.00 monthly, above the 250.00 proof cap."

    post = proof_wiring.make_post(lambda body, sig, path: (Resp(), None),
                                  lambda secret, body: "sig", "secret")
    ok, detail = post("/apply", {"reference": "PROOF-X-20260821T090000"})
    assert ok is False
    assert "above the 250.00 proof cap" in detail


def test_every_handoff_is_signed_and_versioned():
    captured = {}

    def fake_post(body, signature, path):
        captured["body"] = json.loads(body)
        captured["signature"] = signature
        return type("R", (), {"status_code": 200, "text": "{}"})(), None

    post = proof_wiring.make_post(fake_post, lambda secret, body: "SIGNED", "secret")
    post("/provision", {"reference": "PROOF-X-20260821T090000"})

    assert captured["signature"] == "SIGNED"
    assert captured["body"]["contract_version"] == "1.0"
    assert captured["body"]["reference"] == "PROOF-X-20260821T090000"


# --- verify must say WHAT it is verifying ------------------------------------

SANDBOX = {"deployment_target": "oci", "environment_tier": "Development",
           "components": [{"technology_code": "nginx", "size": "small"}]}


def test_the_verification_carries_the_whole_payload_it_was_given():
    """FOUND IN PRODUCTION, PROOF-NGINX-20260821T161305. nginx built cleanly,
    tore down cleanly, and then failed on:

        403: A proof may build only in the sandbox tier (Development); this one
             asked for no tier.

    Because this posted `"policy_input": {}`. Two defects in one line: the tier
    was missing, and the orchestrator derives the resource kinds from that same
    object — so even authorised, it would have asked "is nothing healthy?".
    """
    sent = []

    def post(path, payload):
        sent.append((path, payload))
        return True, json.dumps({"resources": [{"kind": "oci-service-vm",
                                                "state": "healthy"}]})

    handoff = {"resource_kind": "oci-service-vm",
               "resource_kinds": ["oci-service-vm"], "policy_input": SANDBOX}
    healthy, _ = proof_wiring.make_verify(
        post, deadline_seconds=0, sleep=lambda s: None)(
            "PROOF-NGINX-20260821T161305", handoff)

    assert healthy is True
    path, payload = sent[0]
    assert path == "/verify"
    assert payload["policy_input"] == SANDBOX, "the tier and components were dropped"
    # THE SECOND HALF, added 2026-08-22. Composing a payload here instead of
    # forwarding the proof's own is what let /verify ask about a bucket while
    # /apply built a machine: the orchestrator defaults resource_kind to
    # "oci-bucket" when the handoff does not name one, so every proof this
    # project had ever run verified an object store.
    assert payload["resource_kind"] == "oci-service-vm", (
        "verify asked about a different resource kind than the proof built")
    assert payload["proof"] is True


def test_verifying_without_a_policy_input_is_a_visible_break():
    """Not defaulted to {}. A default would have hidden this exact bug: the proof
    reported a failure of nginx when the failure was in the call."""
    with pytest.raises(TypeError):
        proof_wiring.make_verify(_post_returning(True, "{}"), deadline_seconds=0, sleep=lambda s: None)("PROOF-X-20260821T090000")


def test_run_proof_hands_verify_the_same_input_it_built_with():
    """The two must not drift. Verifying a different shape than was built would
    certify something nobody proved."""
    from api import proof as proof_mod
    import inspect

    src = inspect.getsource(proof_mod.run_proof)
    assert "verify(reference, payload)" in src, (
        "verify is no longer given the payload the build used")


# --- it must WAIT for a machine that is still installing ---------------------

def _answers(*bodies):
    """A /verify that returns each body in turn, then repeats the last."""
    seq = list(bodies)
    def post(path, payload):
        body = seq.pop(0) if len(seq) > 1 else seq[0]
        return True, json.dumps(body)
    return post


WAITING = {"checked": 1, "settled": False, "all_ok": False,
           "waiting": ["oci-service-vm"], "resources": [
               {"kind": "oci-service-vm", "expected": True, "state": "waiting"}]}
HEALTHY = {"checked": 1, "settled": True, "all_ok": True, "waiting": [],
           "resources": [{"kind": "oci-service-vm", "expected": True, "state": "ok"}]}


def test_a_machine_still_installing_is_waited_for_not_failed():
    """THE defect this prevents. Terraform returns when the instance reaches
    RUNNING — before cloud-init has installed anything. Asking once finds no
    report and calls a working machine silent, which would refuse to certify
    nginx for being slow to boot rather than for being broken.
    """
    naps = []
    verify = proof_wiring.make_verify(_answers(WAITING, WAITING, HEALTHY),
                                      deadline_seconds=300, interval_seconds=20,
                                      sleep=naps.append)
    healthy, detail = verify("PROOF-NGINX-20260821T161305", SANDBOX)

    assert healthy is True, detail
    assert naps == [20, 20], "it did not wait between attempts"


def test_the_wait_is_bounded_and_says_what_it_waited_for():
    """A proof holds real, billable infrastructure while it waits. It may not
    wait forever, and when it gives up it must name the machine."""
    verify = proof_wiring.make_verify(_answers(WAITING), deadline_seconds=0,
                                      sleep=lambda s: None)
    healthy, detail = verify("PROOF-NGINX-20260821T161305", SANDBOX)

    assert healthy is False
    assert "oci-service-vm" in detail and "report" in detail


def test_a_broken_machine_is_not_waited_out(monkeypatch):
    """Settled and broken is an answer, not a delay. Waiting on it would burn
    fifteen minutes of real infrastructure to learn what it already said."""
    broken = {"checked": 1, "settled": True, "all_ok": False,
              "broken": ["oci-service-vm"], "resources": [
                  {"kind": "oci-service-vm", "expected": True, "state": "broken",
                   "note": "nginx failed to start: port 80 already in use"}]}
    naps = []
    verify = proof_wiring.make_verify(_answers(broken), deadline_seconds=900,
                                      sleep=naps.append)
    healthy, detail = verify("PROOF-NGINX-20260821T161305", SANDBOX)

    assert healthy is False
    assert "port 80 already in use" in detail
    assert naps == [], "it waited on an answer it already had"


def test_something_that_cannot_report_is_not_waited_for_either():
    """A bucket and a managed database file no boot report and never will. The
    build and the teardown ARE the evidence there; waiting would spend the whole
    deadline to learn nothing."""
    naps = []
    verify = proof_wiring.make_verify(
        _answers({"checked": 0, "settled": True, "all_ok": True, "resources": []}),
        deadline_seconds=900, sleep=naps.append)
    healthy, detail = verify("PROOF-PG-20260821T161305", SANDBOX)

    assert healthy is True
    assert naps == []


def test_the_proof_is_no_less_patient_than_a_real_request():
    """If a proof gave up sooner than the poller does, it would refuse to certify
    components that work in production — the worst possible asymmetry, because it
    silently shrinks the catalogue."""
    import inspect
    src = inspect.getsource(proof_wiring.make_verify)
    assert "BOOT_VERIFY_DEADLINE_MINUTES" in src
    assert "BOOT_VERIFY_POLL_SECONDS" in src


# --- "we could not price it" must not read as "it is free" -------------------

def test_a_plan_that_cannot_be_priced_returns_None_not_zero(monkeypatch):
    """FOUND IN PRODUCTION, REQ-2026-0175, 2026-08-21.

    `keycloak` had no blueprint, so its resource kind was unknown and its line
    came back resolved=False — correctly. But the TOTAL was 0.00, and this
    function read only the total, so the proof's cost gate saw "free", approved
    it against a AED 300 ceiling, and went on to build for real.

    A total of 0.00 means two different things. check_cost approves one of them
    and must refuse the other, so the difference has to survive to here.
    """
    from api import proof_wiring as pw

    monkeypatch.setattr(pw.pricing, "estimate_cost",
                        lambda c, t, s: {"unpriced": ["Keycloak"],
                                         "totals": {"monthly": 0.0}})
    assert pw.make_price(None, "oci")([{"technology_code": "keycloak"}]) is None


def test_something_genuinely_free_is_still_priced_at_zero(monkeypatch):
    """The refusal must be about not KNOWING, not about the number being small.
    A component that really costs nothing is priceable and allowed."""
    from api import proof_wiring as pw

    monkeypatch.setattr(pw.pricing, "estimate_cost",
                        lambda c, t, s: {"unpriced": [], "totals": {"monthly": 0.0}})
    assert pw.make_price(None, "oci")([{"technology_code": "x"}]) == 0.0


def test_an_unpriced_component_is_recorded_not_refused():
    """An unpriceable plan no longer refuses, and that is a deliberate trade.

    This asserted `check_cost(None).allowed is False`, reasoning that an unknown
    kind should stop a proof "instead of letting it spend money against a
    meaningless ceiling". THERE IS NO CEILING NOW (2026-08-28, at the reviewer's
    instruction), so the price gates nothing and refusing over a number that
    would not be used is incoherent.

    Three things make this the right way round:

      * the supervisor has already approved the request, with the price — and
        its licence breakdown — shown on the form and in the ticket;
      * the pricing source is LIVE and intermittent. It failed for SQL Server
        twice in twelve hours, and a proof that refuses whenever a rate API
        hiccups is a new way for provisioning to fail;
      * what a proof build costs is minutes of a small machine, not the monthly
        figure this was ever compared against.

    What must NOT happen is claiming a price we do not have, so the verdict says
    so in words and records 0.00 rather than inventing a number.
    """
    from common.proof_rules import check_cost

    verdict = check_cost(None)
    assert verdict.allowed, "an unknown price refuses again, so a flaky rate API blocks proofs"
    assert verdict.monthly == 0.0, "it invented a price it does not have"
    assert "could not be priced" in verdict.reason, "the unknown is not stated"

def test_a_broken_machine_reports_in_its_own_words_not_just_broken():
    """FOUND ON REQ-2026-0177. The keycloak proof failed with "oci-service-vm:
    broken" while the machine had actually said "package keycloak is not
    installed" — the one fact anybody reading the failure needed.

    A machine-backed resource carries `problems`; only the resource-state path
    sets `note`. Reading `note` alone discarded exactly what the boot report
    exists to carry, and the orchestrator's own comment says so: "the machine's
    own words, so the portal can show a human WHY rather than a verdict they
    have to take on trust."
    """
    body = json.dumps({
        "checked": 1, "settled": True, "all_ok": False, "broken": ["oci-service-vm"],
        "resources": [{"kind": "oci-service-vm", "expected": True, "state": "broken",
                       "problems": ["package keycloak is not installed",
                                    "keycloak did not start"]}],
    })
    verify = proof_wiring.make_verify(_post_returning(True, body),
                                      deadline_seconds=0, sleep=lambda s: None)
    healthy, detail = verify("PROOF-KEYCLOAK-X", {"environment_tier": "Development"})

    assert healthy is False
    assert "package keycloak is not installed" in detail, (
        f"the machine's own words were thrown away: {detail!r}")
    assert detail != "oci-service-vm: broken"


def test_the_resource_state_path_still_uses_its_note():
    """A bucket or a cluster has no boot report; its detail arrives as `note`.
    Fixing the machine path must not break that one."""
    body = json.dumps({
        "checked": 1, "settled": True, "all_ok": False, "broken": ["oci-bucket"],
        "resources": [{"kind": "oci-bucket", "expected": True, "state": "broken",
                       "note": "the bucket does not exist"}],
    })
    verify = proof_wiring.make_verify(_post_returning(True, body),
                                      deadline_seconds=0, sleep=lambda s: None)
    healthy, detail = verify("PROOF-X", {"environment_tier": "Development"})

    assert healthy is False
    assert "the bucket does not exist" in detail
