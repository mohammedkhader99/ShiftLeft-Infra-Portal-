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
    verify = proof_wiring.make_verify(_post_returning(True, "not json at all"))
    healthy, detail = verify("PROOF-X-20260821T090000")
    assert healthy is False
    assert "could not be read" in detail


def test_a_broken_resource_is_reported_with_its_own_words():
    body = json.dumps({"resources": [
        {"kind": "oci-oke", "state": "broken", "note": "node pool never reached ACTIVE"}]})
    verify = proof_wiring.make_verify(_post_returning(True, body))
    healthy, detail = verify("PROOF-X-20260821T090000")
    assert healthy is False
    assert "never reached ACTIVE" in detail


def test_still_waiting_is_not_healthy_either():
    """A machine that has not reported yet is unknown, and unknown is not a pass
    — the proof would otherwise tear it down and call it proven."""
    body = json.dumps({"resources": [{"kind": "oci-apache", "state": "waiting"}]})
    verify = proof_wiring.make_verify(_post_returning(True, body))
    assert verify("r")[0] is False


def test_everything_healthy_passes():
    body = json.dumps({"resources": [{"kind": "oci-apache", "state": "ok"}]})
    verify = proof_wiring.make_verify(_post_returning(True, body))
    healthy, detail = verify("r")
    assert healthy is True and "verified healthy" in detail


def test_an_unreachable_orchestrator_is_not_healthy():
    verify = proof_wiring.make_verify(_post_returning(False, "unreachable: timed out"))
    healthy, detail = verify("r")
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
