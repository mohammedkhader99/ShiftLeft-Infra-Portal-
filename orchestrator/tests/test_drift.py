"""Drift detection checks (F-LCM-09): the plan-JSON analysis."""

from orchestrator.drift import detect_drift


def test_no_drift_when_all_no_op():
    plan = {"resource_changes": [
        {"address": "oci_objectstorage_bucket.env", "change": {"actions": ["no-op"]}},
        {"address": "data.oci_objectstorage_namespace.ns", "change": {"actions": ["read"]}},
    ]}
    r = detect_drift(plan)
    assert r["drift"] is False and r["count"] == 0 and r["changes"] == []


def test_drift_on_update():
    plan = {"resource_changes": [
        {"address": "oci_objectstorage_bucket.env", "change": {"actions": ["update"]}},
        {"address": "data.x", "change": {"actions": ["read"]}},
        {"address": "y", "change": {"actions": ["no-op"]}},
    ]}
    r = detect_drift(plan)
    assert r["drift"] is True and r["count"] == 1
    assert r["changes"][0]["address"] == "oci_objectstorage_bucket.env"
    assert r["changes"][0]["actions"] == ["update"]


def test_drift_on_delete_and_replace():
    plan = {"resource_changes": [
        {"address": "a", "change": {"actions": ["delete"]}},
        {"address": "b", "change": {"actions": ["delete", "create"]}},  # replace
    ]}
    assert detect_drift(plan)["count"] == 2


def test_empty_plan_has_no_drift():
    assert detect_drift({})["drift"] is False
