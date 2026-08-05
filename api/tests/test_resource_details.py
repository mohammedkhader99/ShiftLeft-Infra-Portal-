"""What a requester was actually given (F-INT-04).

Terraform has always recorded each resource's OCID, name, private IP and URL, and
the portal has always stored them. Nothing displayed them, so a request reached
'provisioned' and the requester still had to ask an administrator where their
server was — in a portal whose whole purpose is removing that dependency.

Every module names its outputs differently (instance_ocid vs instance_ids vs
instance_ocids), so these tests pin the mapping, and — the one that matters — that
an output nobody has mapped yet is still SHOWN rather than silently dropped.
"""

from types import SimpleNamespace

from api import resource_details


def _res(details, **kw):
    base = dict(kind="oci-apache", name="env-req-apache", region="me-dubai-1",
                lifecycle_state="active", power_state="running", created_at=None,
                details=details)
    base.update(kw)
    return SimpleNamespace(**base)


# --- The shapes real modules emit --------------------------------------------

def test_the_apache_module_plural_outputs():
    """What REQ-2026-0095 actually recorded."""
    out = resource_details.normalise({
        "http_urls": ["http://10.56.43.96/"],
        "https_urls": ["https://10.56.43.96/"],
        "instance_display_names": ["vision-qmg-req-2026-0095-apache-01"],
        "instance_ids": ["ocid1.instance.oc1.me-dubai-1.ansh"],
        "private_ips": ["10.56.43.96"],
        "hostname_labels": ["vision-qmg-req-2026-0095-apache01"],
    })
    assert out["private_ips"] == ["10.56.43.96"]
    assert out["names"] == ["vision-qmg-req-2026-0095-apache-01"]
    assert out["hostnames"] == ["vision-qmg-req-2026-0095-apache01"]
    assert out["urls"] == ["http://10.56.43.96/", "https://10.56.43.96/"]
    assert out["ocids"] == ["ocid1.instance.oc1.me-dubai-1.ansh"]


def test_the_shared_module_singular_outputs():
    """The same concepts under different, singular names — a scalar becomes a
    one-item list so the portal renders every blueprint identically."""
    out = resource_details.normalise({
        "instance_ocid": "ocid1.instance.oc1..x",
        "instance_name": "env-req",
        "instance_private_ip": "10.0.0.5",
    })
    assert out["ocids"] == ["ocid1.instance.oc1..x"]
    assert out["names"] == ["env-req"]
    assert out["private_ips"] == ["10.0.0.5"]


def test_a_bucket_has_a_name_and_nothing_else():
    out = resource_details.normalise({"bucket_name": "env-req-2026-0001"})
    assert out["names"] == ["env-req-2026-0001"]
    assert "private_ips" not in out


# --- The failure this codebase keeps having ----------------------------------

def test_an_output_nobody_mapped_is_shown_not_dropped():
    """A new blueprint's outputs appearing as raw keys is untidy. Vanishing is the
    bug — the portal would look complete while hiding what was built."""
    out = resource_details.normalise({
        "private_ips": ["10.0.0.5"],
        "load_balancer_endpoint": "lb-123.example.internal",
    })
    assert out["other"] == {"load_balancer_endpoint": "lb-123.example.internal"}


def test_build_facts_are_reported_apart_from_identity():
    out = resource_details.normalise({"service_ports": [80], "configured": True,
                                      "private_ips": ["10.0.0.5"]})
    assert out["info"] == {"service_ports": [80], "configured": True}
    assert out["other"] == {}


def test_terraform_empty_values_are_not_shown_as_blanks():
    """Unset optional attributes come back as "" or null; rendering them as empty
    rows would suggest a missing value rather than an absent one."""
    out = resource_details.normalise({
        "private_ips": ["10.0.0.5"], "public_ips": [""], "nsg_id": None,
    })
    assert "public_ips" not in out
    assert out["other"] == {}


def test_malformed_details_never_break_the_request_view():
    for bad in (None, [], "not-a-dict", 42):
        assert resource_details.normalise(bad) == {"other": {}, "info": {}}


# --- The row the portal renders ----------------------------------------------

def test_a_summary_carries_identity_and_lifecycle():
    s = resource_details.summarise(_res({"private_ips": ["10.0.0.5"]}))
    assert s["kind"] == "oci-apache" and s["name"] == "env-req-apache"
    assert s["region"] == "me-dubai-1" and s["lifecycle_state"] == "active"
    assert s["power_state"] == "running"
    assert s["private_ips"] == ["10.0.0.5"]


def test_a_decommissioned_resource_still_reports_what_it_was():
    """Hiding it would make a torn-down environment look as though nothing was
    ever provisioned."""
    s = resource_details.summarise(
        _res({"private_ips": ["10.0.0.5"]}, lifecycle_state="decommissioned"))
    assert s["lifecycle_state"] == "decommissioned"
    assert s["private_ips"] == ["10.0.0.5"]
    # ...but not as still running: the registry keeps the last power state it saw,
    # so REQ-2026-0095 reported "running" for a VM that had been terminated.
    assert s["power_state"] is None
