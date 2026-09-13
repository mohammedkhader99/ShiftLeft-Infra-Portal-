"""`terraform_apply` applies "the EXACT saved plan for this request" — checked.

That sentence was in its docstring while all it did was confirm a file called
`tfplan` existed in the directory it was about to apply. `tfplan` is a filename,
not a claim. Two things write one:

  * a plan for a layout that has since been superseded — a requester changes
    their mind, the new placement is a new version, and "consolidated" writes
    `<ref>/oci-service-vm/tfplan` whichever consolidated layout it was;
  * drift, which runs `plan -out=tfplan` in a provisioned request's own
    workspace to compare against reality, and leaves its result there.

Either would have been applied, and applied successfully: real machines, at a
shape nobody approved, reported against a ticket that says something else. This
is the failure the whole placement design exists to prevent, and the check that
was supposed to stop it was a docstring.

APPLY IS THE ONLY PLACE THIS CAN BE CAUGHT. Plan cannot refuse a plan that does
not exist yet, and the API cannot know what is on the orchestrator's disk. The
orchestrator holds the credentials; the last gate before a real resource is the
one that has to hold.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import provisioner

REF = "REQ-2026-9315"


@pytest.fixture()
def applying(monkeypatch, tmp_path):
    """A workspace apply will reach, with everything before the plan check stubbed.

    The real path needs Terraform, a cloud and credentials. What is under test is
    the gate, so `_workdir` returns tmp_path and `_run` fails loudly if it is ever
    called — reaching Terraform at all would mean the gate let something through.
    """
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "apply")
    monkeypatch.setattr(provisioner, "_require_cloud", lambda *a, **k: None)
    monkeypatch.setattr(provisioner, "_workdir", lambda *a, **k: tmp_path)

    def refuse(*a, **k):
        raise AssertionError("terraform ran: the plan check did not refuse")

    monkeypatch.setattr(provisioner, "_run", refuse)
    return tmp_path


def apply(plan_id: str = "v2"):
    return provisioner.terraform_apply(REF, "env-name", {}, "oci-service-vm",
                                       {}, workspace="oci-service-vm",
                                       plan_id=plan_id)


def save_plan(workdir, plan_id: str | None = "v2"):
    (workdir / provisioner.PLAN_FILE).write_bytes(b"a terraform plan")
    if plan_id is not None:
        provisioner._remember_plan(workdir, REF, "oci-service-vm",
                                   "oci-service-vm", plan_id)


# --- nothing there at all -----------------------------------------------------

def test_no_plan_is_refused(applying):
    """REQ-2026-0315's own message. Correct, and it was the only thing checked."""
    with pytest.raises(provisioner.ProvisionError) as raised:
        apply()

    assert "no saved plan" in str(raised.value)
    assert "approve (plan) it first" in str(raised.value), "it says what to do"


# --- a plan nobody can vouch for ----------------------------------------------

def test_a_plan_with_no_marker_is_refused(applying):
    """THE ONE THAT USED TO BE APPLIED. A bare tfplan in the right directory was
    proof enough. It is proof that a file is there."""
    save_plan(applying, plan_id=None)

    with pytest.raises(provisioner.ProvisionError) as raised:
        apply()

    assert "does not record which approval produced it" in str(raised.value)
    assert "approve (plan) this request again" in str(raised.value)


def test_a_plan_from_a_superseded_layout_is_refused(applying):
    """The requester changed their mind after the plan was made. Both layouts
    write the same filename in the same directory."""
    save_plan(applying, plan_id="v1")

    with pytest.raises(provisioner.ProvisionError) as raised:
        apply(plan_id="v2")

    assert "layout v1" in str(raised.value) and "layout v2" in str(raised.value)


def test_the_refusal_names_both_layouts_not_just_that_they_differ(applying):
    """"Stale plan" sends somebody to look for which one. The version they
    approved and the version on disk are the two facts that end the search."""
    save_plan(applying, plan_id="v7")

    with pytest.raises(provisioner.ProvisionError) as raised:
        apply(plan_id="v9")

    message = str(raised.value)
    assert "v7" in message and "v9" in message
    assert "what gets built is what was approved" in message


def test_a_plan_drift_left_behind_is_refused(applying):
    """Drift overwrites the approved plan with its own. `_forget_plan` is what
    makes the difference visible here rather than at a machine's console."""
    save_plan(applying, plan_id="v2")
    provisioner._forget_plan(applying)          # what terraform_drift does

    with pytest.raises(provisioner.ProvisionError) as raised:
        apply(plan_id="v2")

    assert "does not record which approval produced it" in str(raised.value)


def test_an_unplaced_request_cannot_apply_a_placed_requests_plan(applying):
    """Empty matches only empty. A legacy request has no placement and no plan
    id, and must not pick up a plan made for version 1 of anything."""
    save_plan(applying, plan_id="v1")

    with pytest.raises(provisioner.ProvisionError):
        apply(plan_id="")


# --- and the plan that IS the approved one goes through -----------------------

def test_the_matching_plan_is_applied(applying, monkeypatch):
    """The gate has to let the right plan through, or the fix is an outage.

    Terraform is stubbed from here on: a successful init, apply and output, in
    the order the real one runs them.
    """
    save_plan(applying, plan_id="v2")
    ran: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "Apply complete! Resources: 3 added, 0 changed, 0 destroyed."
        stderr = ""

    class Outputs(Done):
        stdout = json.dumps({"instance_id": {"value": "ocid1.instance.x"}})

    def record(args, workdir, timeout=0):
        ran.append(args)
        return Outputs() if args[0] == "output" else Done()

    monkeypatch.setattr(provisioner, "_run", record)
    result = apply(plan_id="v2")

    assert [a[0] for a in ran] == ["init", "apply", "output"]
    assert provisioner.PLAN_FILE in ran[1], "it applies the SAVED plan, not a fresh one"
    assert result["summary"].startswith("Apply complete!")
    assert result["outputs"] == {"instance_id": "ocid1.instance.x"}


def test_an_unplaced_request_with_an_unplaced_plan_still_applies(applying, monkeypatch):
    """Every request raised before placement existed."""
    save_plan(applying, plan_id="")

    class Done:
        returncode = 0
        stdout = "Apply complete!"
        stderr = ""

    monkeypatch.setattr(provisioner, "_run", lambda *a, **k: Done())
    assert provisioner.terraform_apply(REF, "env-name", {}, "oci-service-vm", {},
                                       workspace="oci-service-vm", plan_id="")


# --- the gate cannot be reached with apply switched off -----------------------

def test_apply_is_still_refused_outright_when_not_enabled(applying, monkeypatch):
    """Checked before any of this. A saved plan and a matching id must not be a
    way past PROVISION_MODE."""
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "plan")
    save_plan(applying, plan_id="v2")

    with pytest.raises(provisioner.ProvisionError) as raised:
        apply(plan_id="v2")

    assert "apply is not enabled" in str(raised.value)
