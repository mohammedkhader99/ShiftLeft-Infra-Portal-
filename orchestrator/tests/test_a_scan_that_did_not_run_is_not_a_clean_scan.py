"""A stack's IaC verdict must say when part of it was never checked.

H.11 was one function returning `ok: True, high: 0` because it crashed and the
crash was swallowed. This is the same shape one layer up. `_merge_scans` combined
a stack's per-workspace scans and kept `findings`, `counts`, `high` and `ok` —
dropping the only two fields that say a verdict is incomplete:

    error             set by _scan_saved_plan when the scan could not run
    unreviewed_types  set by the scanner for resource types it has no rules for

Found on 2026-09-13 in the same reading as H.11, and it matters because it would
have hidden H.11's successor: after the NameError was fixed, a scan failing for
any other reason — terraform missing, a plan file that will not parse, a timeout
on a cluster's plan — merged to a clean verdict again, with nothing to read.

The second field is not hypothetical either. REQ-2026-0315's service VM creates
`oci_core_volume` and `oci_core_volume_attachment`, and the scanner has no rules
for either. It reported that honestly. The merge threw it away.

NOTHING HERE BLOCKS A PLAN, and that is deliberate. The gate reads `high`, and a
scan that could not run finds nothing high. "Never breaks the plan" is the right
call — which is exactly why the failure has to be visible instead of absorbed.
"""

from __future__ import annotations

import orchestrator.main as omain

CLEAN = {"findings": [], "counts": {"high": 0, "medium": 0, "low": 0},
         "high": 0, "ok": True}
CRASHED = {**CLEAN, "error": "name 'tmo' is not defined"}
UNREVIEWED = {**CLEAN, "unreviewed_types": ["oci_core_volume",
                                            "oci_core_volume_attachment"]}
HIGH = {"findings": [{"severity": "high", "message": "public bucket"}],
        "counts": {"high": 1, "medium": 0, "low": 0}, "high": 1, "ok": False}


# --- a scan that failed --------------------------------------------------------

def test_a_stack_where_one_scan_crashed_is_not_ok():
    """THE DEFECT. Two workspaces, one scanned clean and one that never ran, and
    the verdict said everything was fine."""
    merged = omain._merge_scans([("oci-service-vm", CLEAN), ("oci-oke", CRASHED)])

    assert merged["ok"] is False
    assert merged["errors"], "the failure left no trace in the merged verdict"


def test_the_failure_names_the_workspace_it_happened_in():
    """"terraform show failed" sends somebody looking through a stack for which
    plan it was."""
    merged = omain._merge_scans([("oci-service-vm", CLEAN), ("oci-oke", CRASHED)])

    assert merged["errors"] == ["oci-oke: name 'tmo' is not defined"]


def test_every_failure_is_kept_not_just_the_first():
    merged = omain._merge_scans([
        ("oci-oke", {**CLEAN, "error": "terraform show failed"}),
        ("oci-postgres", {**CLEAN, "error": "timed out"}),
        ("oci-service-vm", CLEAN),
    ])

    assert len(merged["errors"]) == 2
    assert merged["errors"] == ["oci-oke: terraform show failed",
                               "oci-postgres: timed out"]


def test_a_plan_with_no_scan_at_all_is_a_failure_too():
    """It used to be filtered out at the call site — `if p.get("scan")` — which
    is the same silence somewhere else."""
    merged = omain._merge_scans([("oci-service-vm", CLEAN), ("oci-oke", {})])

    assert merged["ok"] is False
    assert merged["errors"] == ["oci-oke: no scan result"]


# --- and it still does not block -----------------------------------------------

def test_a_failed_scan_finds_nothing_high():
    """The gate reads `high`. A scan that could not run must not manufacture a
    finding to stop a plan that may be perfectly good."""
    merged = omain._merge_scans([("oci-oke", CRASHED)])

    assert merged["high"] == 0
    assert merged["findings"] == []


# --- types nothing has rules for -----------------------------------------------

def test_resource_types_with_no_rules_survive_the_merge():
    """REQ-2026-0315's own service VM. "We have no rules for this" must not
    arrive as "we checked this"."""
    merged = omain._merge_scans([("oci-service-vm", UNREVIEWED), ("oci-oke", CLEAN)])

    assert merged["unreviewed_types"] == ["oci_core_volume",
                                          "oci_core_volume_attachment"]


def test_unreviewed_types_are_pooled_across_the_stack_without_duplicates():
    merged = omain._merge_scans([
        ("a", {**CLEAN, "unreviewed_types": ["oci_core_volume", "oci_psql_db_system"]}),
        ("b", {**CLEAN, "unreviewed_types": ["oci_core_volume"]}),
    ])

    assert merged["unreviewed_types"] == ["oci_core_volume", "oci_psql_db_system"]


def test_having_no_rules_for_a_type_is_not_a_scan_failure():
    """Different things. A type nothing has rules for was still looked at by a
    scan that ran; conflating the two would make every stack permanently not-ok
    and teach everyone to ignore the field."""
    merged = omain._merge_scans([("oci-service-vm", UNREVIEWED)])

    assert merged["ok"] is True
    assert merged["errors"] == []


# --- the happy path is unchanged ------------------------------------------------

def test_a_stack_that_scanned_clean_is_ok():
    merged = omain._merge_scans([("oci-oke", CLEAN), ("oci-service-vm", CLEAN)])

    assert merged["ok"] is True
    assert merged["high"] == 0
    assert merged["errors"] == []
    assert merged["unreviewed_types"] == []


def test_findings_and_counts_still_add_up():
    merged = omain._merge_scans([("oci-oke", HIGH), ("oci-service-vm", CLEAN)])

    assert merged["high"] == 1
    assert merged["counts"]["high"] == 1
    assert len(merged["findings"]) == 1


def test_a_high_finding_is_not_ok_even_when_every_scan_ran():
    merged = omain._merge_scans([("oci-oke", HIGH)])

    assert merged["ok"] is False
    assert merged["errors"] == []


def test_nothing_to_merge_stays_empty():
    """No plans means no verdict, which is not the same as a clean one. The
    caller sends this straight through to the gate, which reads `high` with a
    default of 0 — so an empty dict has to keep meaning "nothing was planned"."""
    assert omain._merge_scans([]) == {}


def test_the_fields_are_always_there_when_there_is_a_verdict():
    """A stable shape. `errors` present and empty is a fact; `errors` missing is
    a question about whether anything looked — which is the whole subject here."""
    merged = omain._merge_scans([("oci-oke", CLEAN)])

    for field in ("findings", "counts", "high", "ok", "errors", "unreviewed_types"):
        assert field in merged, field
