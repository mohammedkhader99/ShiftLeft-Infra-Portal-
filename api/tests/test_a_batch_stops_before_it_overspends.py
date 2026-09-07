"""A batch prover spends real money, so its limits are what needs testing.

Certifying one technology costs two or three real machines. A tool that walks a
list of thirty is therefore a tool that can spend ninety, overnight, while
nobody is watching -- which is exactly the situation in which a cap that is
checked in the wrong place, or a leak that goes unnoticed, becomes expensive.

The arithmetic and the stop conditions are pure functions for that reason: the
half that decides how much money is spent can be tested without a cloud, and is.
"""

from __future__ import annotations

from ops.batch_prove import (MACHINES_PER_CANDIDATE, Budget, catalogue_entry,
                             plan, stop_reason)


# --- the cap ---------------------------------------------------------------------

def test_the_cap_is_checked_before_starting_not_after():
    """A cap checked after a candidate runs is a cap already exceeded. With two
    machines left and three needed, the answer is no."""
    budget = Budget(cap=6)
    budget.spend(4)

    assert budget.left == 2
    assert budget.affords(3) is False


def test_a_batch_reserves_the_worst_case_per_candidate():
    """Reserved on what a candidate COULD cost, not what it usually does. A
    container rung alone is two machines -- the question pass and the narrowed
    pass -- and a refused package rung ahead of it makes three."""
    out = plan(["a", "b", "c"], certified=set(), cap=6)

    assert out["attempt"] == ["a", "b"]
    assert ("c", "would exceed the cap of 6 machine(s)") in out["skipped"]
    assert out["reserved"] == 2 * MACHINES_PER_CANDIDATE


def test_a_cap_too_small_for_even_one_attempts_nothing():
    out = plan(["a"], certified=set(), cap=MACHINES_PER_CANDIDATE - 1)

    assert out["attempt"] == []


def test_spending_never_goes_backwards():
    """A candidate that consumed no machine must not hand budget back: the
    reservation is the protection, and returning it would let a long tail of
    cheap failures fund an expensive one."""
    budget = Budget(cap=3)
    budget.spend(0)
    budget.spend(-5)

    assert budget.spent == 0
    assert budget.left == 3


# --- what it declines to do ------------------------------------------------------

def test_what_is_already_certified_is_not_re_proved():
    """A re-run must cost nothing for work already done, so a night that stopped
    half way can be picked up the next evening."""
    out = plan(["mysql", "grafana"], certified={"mysql"}, cap=99)

    assert out["attempt"] == ["grafana"]
    assert ("mysql", "already certified") in out["skipped"]


def test_a_repeated_candidate_is_only_paid_for_once():
    out = plan(["grafana", "grafana", "GRAFANA "], certified=set(), cap=99)

    assert out["attempt"] == ["grafana"]


def test_blank_entries_are_ignored_rather_than_attempted():
    """An edited list with a stray comma must not buy a machine for an empty
    name."""
    out = plan(["", "  ", "grafana"], certified=set(), cap=99)

    assert out["attempt"] == ["grafana"]


# --- when it must not run at all -------------------------------------------------

def test_it_will_not_start_while_a_request_is_executing():
    """The check made by hand before every proof this project has run."""
    assert "executing" in stop_reason(executing=1, proofs_running=0, leaked=[])


def test_it_will_not_start_while_another_proof_is_running():
    assert "already running" in stop_reason(executing=0, proofs_running=1, leaked=[])


def test_an_abandoned_proof_stops_the_whole_batch():
    """THE ONE THAT MATTERS OVERNIGHT. `proof.run_proof` records `abandoned`
    when a machine was built and the teardown did not take -- "something may
    still be running under <reference>" in its own words. Every further
    candidate would add to a leak nobody sees until morning. Stop, and name it.

    WHAT THIS REPLACED, and why it is worth remembering. The first version
    asked the CLOUD which proof machines were still alive. The API holds no
    cloud credential by design -- `oci` is not even installed in its image --
    so that check hit its own exception handler and returned "no leak" every
    time it ran. It was caught before the first batch by asking whether an
    empty list meant "nothing leaked" or "I could not look". A safety check
    that cannot fail is worse than none, because it is believed."""
    why = stop_reason(executing=0, proofs_running=0,
                      leaked=["PROOF-GRAFANA-20260906T010101-AAA111"])

    assert "abandoned" in why
    assert "PROOF-GRAFANA-20260906T010101-AAA111" in why


def test_a_clear_field_does_not_stop_it():
    assert stop_reason(executing=0, proofs_running=0, leaked=[]) == ""


def test_the_checks_are_ordered_so_the_loudest_is_reported_first():
    """All three at once is possible after a bad night. A request executing is
    the one that must be named, because it means a person is waiting."""
    why = stop_reason(executing=2, proofs_running=1, leaked=["x"])

    assert "executing" in why


# --- what it writes into the catalogue -------------------------------------------
#
# Added 2026-09-05 when the reviewer asked for proved technologies to be listed
# immediately. The rule this reverses -- "proving is not offering" -- was about
# listing the UNPROVED, and nothing unproved is listed here. What must hold is
# that an entry written without a person reading it first is still true, and
# still complete.

MARIADB = {
    "code": "mariadb", "builds_on": "oci/service-vm", "ports": [3306],
    "container": {"image": "docker.io/library/mariadb", "tag": "latest",
                  "digest": "sha256:" + "c" * 64,
                  "environment": {"MARIADB_RANDOM_ROOT_PASSWORD": "yes"}},
    "rhel": {"packages": [], "services": ["mariadb"]},
}


def test_the_entry_says_what_the_machine_proved():
    entry = catalogue_entry("mariadb", "MariaDB", MARIADB, "PROOF-MARIADB-1")

    assert entry["code"] == "mariadb"
    assert entry["name"] == "MariaDB"
    assert entry["lifecycle_state"] == "certified"
    assert "3306" in entry["note"]
    assert "docker.io/library/mariadb" in entry["note"]
    assert "PROOF-MARIADB-1" in entry["note"]


def test_the_group_is_read_from_the_recipe_not_the_name():
    """Inferring delivery from a code name is what called OCI's managed
    PostgreSQL "software". Everything this tool proves runs on a machine of its
    own, and that is a fact about the recipe."""
    assert catalogue_entry("x", "X", MARIADB, "P")["delivery_model"] == "software"


def test_the_note_fits_the_column_it_goes_in():
    """`TechnologyDelivery.note` is String(300). SQLite ignores that and Postgres
    enforces it, so a long note passes every test here and fails on the live
    database -- which is exactly what happened to Oracle Free's note, caught by
    a guard rather than by a person."""
    wordy = {**MARIADB, "ports": list(range(1000, 1040))}

    assert len(catalogue_entry("x", "A Name" * 20, wordy, "P" * 80)["note"]) <= 300


def test_a_recipe_with_no_ports_still_produces_a_usable_note():
    """A runtime -- dotnet, a JVM -- serves nothing and is still worth listing."""
    runtime = {"code": "x", "ports": [], "rhel": {"packages": ["x"], "services": []}}

    note = catalogue_entry("x", "X", runtime, "P")["note"]

    assert "listening" not in note
    assert "P" in note


def test_the_name_falls_back_to_the_code_rather_than_being_invented():
    """A candidate nobody named is still proved and still offered; it carries
    its code until a person improves it. Inventing a display name by
    title-casing is the guess this project keeps removing."""
    assert catalogue_entry("nats", "", MARIADB, "P")["name"] == "nats"


# --- the note is what a requester reads ---------------------------------------
#
# FOUND 2026-09-07, by reading the nine notes the first batches actually wrote.
# `_offer` passed `blueprint.notes` -- a whole certification SENTENCE -- into the
# slot catalogue_entry formats as "Proved by {…}", so every note read
#
#   Proved by Certified automatically by proof build PROOF-GITEA-…: built,
#   verified healthy and destroyed.: the portal built it, …
#
# doubled and ungrammatical, and long enough that String(300) then cut the image
# name off the end: memcached's note stopped at "docker.io/library/memcache" and
# prometheus's at "quay.io/prometheus/promet".
#
# Neither half was caught by a test, because the tests passed a short reference
# ("PROOF-MARIADB-1") that no caller ever supplies.

#: WHAT `_offer` ACTUALLY PASSED -- a whole certification sentence, 114
#: characters of it, in the slot formatted as "Proved by {…}". Using a tidy
#: "PROOF-MARIADB-1" here is why every earlier test passed: at that length the
#: note fits whichever order its parts are in, so nothing was being tested.
A_SENTENCE_WHERE_A_REFERENCE_BELONGS = (
    "Certified automatically by proof build "
    "PROOF-MEMCACHED-20260906T023847-213E21: built, verified healthy and "
    "destroyed.")

LONG_REFERENCE = "PROOF-MEMCACHED-20260906T023847-213E21"


def test_the_image_survives_even_an_absurdly_long_reference():
    """THE DEFECT, at the size it really arrived. String(300) cuts the TAIL, so
    whatever is last is what is lost -- and the image name is the one fact a
    reader cannot reconstruct, while a proof reference is still in the audit log
    and in certification_proof. memcached's note stopped at
    "docker.io/library/memcache" and prometheus's at "quay.io/prometheus/promet"."""
    wordy = {**MARIADB, "container": {**MARIADB["container"],
                                      "image": "docker.io/library/memcached"}}

    note = catalogue_entry("memcached", "Memcached", wordy,
                           A_SENTENCE_WHERE_A_REFERENCE_BELONGS)["note"]

    assert len(note) <= 300
    assert "docker.io/library/memcached." in note, (
        f"the image name was truncated mid-word: {note!r}")


def test_a_realistic_reference_still_leaves_room_for_everything():
    """Every part a listing needs, at the sizes the real thing produces."""
    note = catalogue_entry("prometheus", "Prometheus",
                           {**MARIADB, "ports": [9090],
                            "container": {"image": "quay.io/prometheus/prometheus"}},
                           "PROOF-PROMETHEUS-20260906T034643-A4BD50")["note"]

    assert "Prometheus" in note
    assert "9090" in note
    assert "quay.io/prometheus/prometheus." in note
    assert "PROOF-PROMETHEUS-20260906T034643-A4BD50" in note
    assert len(note) <= 300
