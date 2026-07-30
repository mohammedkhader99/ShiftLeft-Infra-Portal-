"""IaC scanner checks (F-SEC-03/04): the ruleset over a terraform plan JSON."""

from orchestrator.scanner import scan_plan

GOOD_TAGS = {"managed_by": "infra-portal", "reference": "REQ-1",
             "cost_centre": "IMD-1001", "classification": "internal"}


def _plan(after: dict) -> dict:
    return {"resource_changes": [{
        "address": "oci_objectstorage_bucket.env",
        "type": "oci_objectstorage_bucket",
        "change": {"actions": ["create"], "after": after},
    }]}


def test_compliant_bucket_has_no_findings():
    res = scan_plan(_plan({"access_type": "NoPublicAccess", "freeform_tags": GOOD_TAGS,
                           "versioning": "Enabled"}))
    assert res["ok"] is True and res["high"] == 0 and res["findings"] == []


def test_public_bucket_is_high():
    res = scan_plan(_plan({"access_type": "ObjectRead", "freeform_tags": GOOD_TAGS,
                           "versioning": "Enabled"}))
    assert res["high"] == 1 and res["ok"] is False
    assert any(f["rule"] == "bucket-public-access" for f in res["findings"])


def test_missing_tags_is_medium():
    res = scan_plan(_plan({"access_type": "NoPublicAccess",
                           "freeform_tags": {"managed_by": "x"}, "versioning": "Enabled"}))
    assert res["counts"]["medium"] == 1 and res["high"] == 0
    f = next(f for f in res["findings"] if f["rule"] == "bucket-missing-tags")
    assert "cost_centre" in f["message"] and "classification" in f["message"]


def test_sensitive_data_without_cmk_is_high():
    tags = {**GOOD_TAGS, "classification": "restricted"}
    res = scan_plan(_plan({"access_type": "NoPublicAccess", "freeform_tags": tags,
                           "versioning": "Enabled"}), classification="restricted")
    assert res["high"] == 1
    assert any(f["rule"] == "bucket-no-cmk" for f in res["findings"])
    # A customer-managed key clears it.
    ok = scan_plan(_plan({"access_type": "NoPublicAccess", "freeform_tags": tags,
                          "versioning": "Enabled", "kms_key_id": "ocid1.key.oc1.."}),
                   classification="restricted")
    assert not any(f["rule"] == "bucket-no-cmk" for f in ok["findings"])


def test_versioning_disabled_is_low():
    res = scan_plan(_plan({"access_type": "NoPublicAccess", "freeform_tags": GOOD_TAGS}))
    assert res["counts"]["low"] == 1 and res["high"] == 0
    assert any(f["rule"] == "bucket-versioning-disabled" for f in res["findings"])


def test_deletes_and_unknown_types_ignored():
    plan = {"resource_changes": [
        {"type": "oci_objectstorage_bucket", "change": {"actions": ["delete"], "after": None}},
        {"type": "some_other_resource", "change": {"actions": ["create"], "after": {"x": 1}}},
    ]}
    res = scan_plan(plan)
    assert res["findings"] == [] and res["ok"] is True


def test_all_defaults_safe_access_but_flags_tags_and_versioning():
    res = scan_plan(_plan({"name": "b"}))  # minimal after: everything defaulted
    rules = {f["rule"] for f in res["findings"]}
    assert "bucket-public-access" not in rules      # default access is private
    assert "bucket-missing-tags" in rules
    assert "bucket-versioning-disabled" in rules
    assert res["high"] == 0
