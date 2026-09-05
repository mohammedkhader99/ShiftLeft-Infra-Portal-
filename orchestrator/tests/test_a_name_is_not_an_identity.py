"""Someone else's machine was nearly terminated because it was NAMED like ours.

For a week a sweep reported, every time it ran:

    PROOF machines still alive: ['proof-oci-compute-req-2026-0097-instance']

It selected on the name -- anything containing "proof" -- so the machine was
reported as our litter, investigated as our litter, and on 2026-09-05 came
within one command of being terminated as our litter. Its
`Oracle-Tags.CreatedBy` is `default/atul.kumar@noqodi.com`. This deployment
authenticates as `mohammed.khader@emaratechg.ae`. It was never ours.

Three things made it look like ours, and no one of them was enough alone: it is
NAMED like a proof machine; it is TAGGED `managed_by=infra-portal`, because the
other deployment runs this same portal code; and it carries
`reference=REQ-2026-0097`, a reference THIS portal also issued -- for a
different machine, destroyed cleanly on 5 August.

The compartment is shared. Of eleven live instances, ten belong to that other
principal. Ownership is therefore decided by the credential Oracle recorded as
having created the machine, which naming cannot forge.

The fixtures below are the real tenancy, read on 2026-09-05.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ops.live_instances import classify, created_by, reference_of

OURS = "mohammed.khader@emaratechg.ae"
THEIRS = "atul.kumar@noqodi.com"


class Instance:
    """Only the attributes the classifier reads, shaped as the OCI SDK returns."""

    def __init__(self, name, *, creator=None, reference="", state="RUNNING",
                 managed_by="infra-portal"):
        self.display_name = name
        self.lifecycle_state = state
        self.time_created = datetime(2026, 9, 2, 8, 16, tzinfo=timezone.utc)
        self.freeform_tags = {"reference": reference, "managed_by": managed_by}
        self.defined_tags = ({"Oracle-Tags": {"CreatedBy": f"default/{creator}"}}
                             if creator else {})


#: The machine itself, exactly as the tenancy reports it.
THE_LOOKALIKE = Instance("proof-oci-compute-req-2026-0097-instance",
                         creator=THEIRS, reference="REQ-2026-0097")
#: A real proof machine of ours: the reference names the PROOF, not a request.
OUR_PROOF = Instance("proof-mysql-20260904t224500-93d-01", creator=OURS,
                     reference="PROOF-MYSQL-20260904T224500-93D545")
#: An ordinary machine of ours, built for a request. Not a stray.
OUR_REQUEST_VM = Instance("test-req-2026-0134-apache-01", creator=OURS,
                          reference="REQ-2026-0134")


def test_a_machine_named_like_a_proof_is_not_ours_if_someone_else_made_it():
    """THE DEFECT, and the whole reason this file exists."""
    out = classify([THE_LOOKALIKE], OURS)

    assert out["mine"] == []
    assert out["theirs"] == [THE_LOOKALIKE]
    assert out["strays"] == [], (
        "someone else's machine was reported as our leaking proof, which is how "
        "it came within one command of being terminated")


def test_the_portal_tag_does_not_make_it_ours():
    """`managed_by=infra-portal` says a portal built it, not that OURS did. The
    other deployment runs this same code and stamps the same tag."""
    assert created_by(THE_LOOKALIKE) == THEIRS
    assert THE_LOOKALIKE.freeform_tags["managed_by"] == "infra-portal"

    assert classify([THE_LOOKALIKE], OURS)["mine"] == []


def test_a_colliding_reference_does_not_make_it_ours():
    """It carries REQ-2026-0097, and this portal really did issue REQ-2026-0097
    -- for a different machine that was destroyed on 5 August. A reference is
    unique within a deployment, not across a shared compartment."""
    assert reference_of(THE_LOOKALIKE) == "REQ-2026-0097"

    assert classify([THE_LOOKALIKE, OUR_REQUEST_VM], OURS)["mine"] == [OUR_REQUEST_VM]


def test_our_own_stray_proof_is_still_caught():
    """The point of the sweep must survive the fix. A proof of ours still
    running is money leaking, and it is found by the reference the PORTAL wrote,
    not by the name."""
    out = classify([OUR_PROOF, THE_LOOKALIKE, OUR_REQUEST_VM], OURS)

    assert out["strays"] == [OUR_PROOF]


def test_our_own_machine_named_proof_of_concept_is_not_litter():
    """FOUND BY A PLANT, 2026-09-05, and the case an operator will actually meet.

    "Proof of concept" is one of the commonest environment names in this
    business. A requester who calls their environment `proof-of-concept` gets a
    machine named `proof-of-concept-req-2026-0300-apache-01`: OURS, legitimate,
    meant to be running, and containing the word "proof".

    Selecting strays by name flags it as leaking money and invites an operator
    to destroy a user's environment. The reference the PORTAL wrote says what it
    is -- a request, not a proof -- and that is the claim to trust, for the same
    reason the ownership check trusts Oracle's CreatedBy over a display name."""
    poc = Instance("proof-of-concept-req-2026-0300-apache-01", creator=OURS,
                   reference="REQ-2026-0300")

    out = classify([poc], OURS)

    assert out["mine"] == [poc], "a machine we built was not recognised as ours"
    assert out["strays"] == [], (
        "a user's proof-of-concept environment was reported as a leaking proof "
        "machine, which invites an operator to destroy it")


def test_a_request_machine_of_ours_is_not_a_stray():
    """Ours, and meant to be running. Only proof machines are litter."""
    assert classify([OUR_REQUEST_VM], OURS)["strays"] == []


def test_a_terminated_machine_is_not_alive():
    for state in ("TERMINATED", "TERMINATING", "terminated"):
        gone = Instance("proof-mysql-x-01", creator=OURS, reference="PROOF-X",
                        state=state)
        out = classify([gone], OURS)
        assert out["mine"] == [] and out["strays"] == [], state


def test_a_machine_with_no_creator_tag_is_not_quietly_counted_as_ours():
    """Absent is not the same as mine. This whole file exists because something
    that looked like ours was not, and a missing tag is less evidence than a
    wrong one, never more."""
    untagged = Instance("proof-something-01", reference="PROOF-SOMETHING")

    out = classify([untagged], OURS)

    assert out["mine"] == []
    assert out["strays"] == []
    assert out["unattributed"] == [untagged]


def test_nothing_is_hidden_from_the_operator():
    """The fix must not become "filter out what is not ours". That the
    compartment is SHARED is the fact the broken version obscured, and the one
    an operator most needs."""
    out = classify([THE_LOOKALIKE, OUR_PROOF, Instance("mystery-01")], OURS)

    assert len(out["mine"]) + len(out["theirs"]) + len(out["unattributed"]) == 3


def test_the_real_tenancy_reads_the_way_it_actually_did():
    """Eleven live instances on 2026-09-05: ten another principal's, one an OKE
    nodepool's, none ours -- which is the CORRECT result, because our proof
    machines are destroyed."""
    nodepool = Instance("oke-cutzjdmy2la-npnenunakza-swj5pmd3rma-0",
                        creator="ocid1.nodepool.oc1.me-dubai-1.aaa", reference="")
    theirs = [Instance(f"dev-noqodi-26-00{n}-oke-bastion", creator=THEIRS,
                       reference=f"REQ-2026-00{n}") for n in range(21, 30)]

    out = classify([*theirs, THE_LOOKALIKE, nodepool], OURS)

    assert out["mine"] == []
    assert out["strays"] == []
    assert len(out["theirs"]) == len(theirs) + 2
