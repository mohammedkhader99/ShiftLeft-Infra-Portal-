"""Triage must not tell somebody to retry what can never succeed.

REQ-2026-0314, 2026-09-12. Its placement put three workloads on a cluster and
the orchestrator refused — immediately, deliberately, and permanently. "Diagnose
failure" reported:

    Likely cause(s):  The orchestrator could not be reached — a transient
                      infrastructure/network issue.
    Next steps:       Check the orchestrator service health and retry; the
                      background poller will also re-attempt on its next cycle.

Every clause of that is wrong. The orchestrator answered in under a second. The
refusal is permanent until a network route exists. Retrying cannot work, and
checking the service's health sends somebody to inspect a component that is
doing exactly its job.

IT MATCHED ON THE WORD "orchestrator". The refusal says "the orchestrator has no
route to it", and the unreachable class listed `orchestrator` as a keyword — so
the name of the component appearing in a message was read as evidence about its
availability. That word appears in refusals, policy messages and half the status
details this portal writes.

An explanation a requester acts on is worse than no explanation when it is
wrong: it costs them the retry, and then the wait, and then somebody else's time
looking at a healthy service.
"""

from __future__ import annotations

import pytest

from api import ai_triage

# The real sentence, as the orchestrator sends it. Shortened only where this
# file would otherwise wrap badly; the matched phrases are verbatim.
CLUSTER_REFUSAL = (
    "This cannot be built: nothing in this system deploys into a cluster — the "
    "Kubernetes API endpoint is private and the orchestrator has no route to it "
    "— so building it would provision machines instead of the cluster that was "
    "asked for. Choose a layout that builds machines, or ask for the cluster on "
    "its own and deploy into it yourself. (Placement version 1 puts 3 workload "
    "group(s) on a cluster: cluster-postgres16, cluster-minio, cluster-oracle-db.)"
)

UNREACHABLE = (
    "Could not reach the orchestrator at http://orchestrator:9091 — connection "
    "refused after 5s."
)


def classify(text: str) -> list[tuple[str, str]]:
    """(cause, step) for every keyword class this text matches, in order."""
    lowered = text.lower()
    return [(cause, step)
            for keywords, cause, step in ai_triage._KEYWORD_CLASSES
            if any(k in lowered for k in keywords)]


def test_there_are_classes_to_match():
    """A list that emptied would make every test below pass vacuously."""
    assert len(ai_triage._KEYWORD_CLASSES) >= 6


# --- the refusal ---------------------------------------------------------------

def test_a_cluster_refusal_is_recognised_as_a_refusal():
    causes = [cause for cause, _ in classify(CLUSTER_REFUSAL)]

    assert causes, "the refusal matched no class at all"
    assert "deliberate refusal, not a fault" in causes[0], causes


def test_it_does_not_tell_anybody_to_retry():
    """The costly half. A retry cannot work until a network route exists, and
    being told to try again is how somebody spends an afternoon on it."""
    _, step = classify(CLUSTER_REFUSAL)[0]

    assert "Retrying will not help" in step
    assert "Choose a layout that builds machines" in step, "it names the way out"


def test_it_is_not_read_as_the_orchestrator_being_down():
    """THE ACTUAL DEFECT. The refusal names the orchestrator; that is not a
    statement about whether it answered."""
    first_cause = classify(CLUSTER_REFUSAL)[0][0]

    assert "could not be reached" not in first_cause
    assert "transient" not in first_cause


def test_the_word_orchestrator_alone_proves_nothing():
    """It appears in refusals, policy messages and half the status details here.
    Matching on it turned all of them into "the network is flaky, try again"."""
    for keywords, cause, _ in ai_triage._KEYWORD_CLASSES:
        if "could not be reached" in cause:
            assert "orchestrator" not in keywords, (
                "the unreachable class still treats the component's NAME as "
                "evidence about its availability")


# --- and a real outage is still diagnosed as one -------------------------------

def test_an_orchestrator_that_really_is_down_is_still_recognised():
    """The fix must not have made the portal blind to the thing the class was
    written for."""
    causes = [cause for cause, _ in classify(UNREACHABLE)]

    assert any("could not be reached" in c for c in causes), causes


@pytest.mark.parametrize("text", [
    "Connection timed out talking to the orchestrator.",
    "orchestrator unreachable",
    "connection refused",
])
def test_the_language_of_not_reaching_something_still_matches(text):
    assert any("could not be reached" in cause for cause, _ in classify(text)), text


def test_a_refusal_and_an_outage_are_never_the_same_answer():
    """If one text produced both, the first-match ordering would decide by
    accident rather than by meaning."""
    refusal = {cause for cause, _ in classify(CLUSTER_REFUSAL)}
    outage = {cause for cause, _ in classify(UNREACHABLE)}

    assert not (refusal & outage), sorted(refusal & outage)
