"""Live cloud state could not see a single one of this estate's servers.

`_is_compute` decided, from the NAME, whether to ask OCI's compute API or its
object storage API. It matched "instance", "compute", "vm" and "oci-vm" — and
`oci-service-vm` is none of those, because "vm" was an EXACT match and not a
suffix.

    oci-service-vm   24 resources   INVISIBLE
    oci-apache       13 resources   INVISIBLE
    oci-kafka                       INVISIBLE

That is every VM in the estate carrying software — nginx, redis, .NET, Vault,
SQL Server, Apache. Reconcile asked object storage for a bucket named after a
machine, got a 404, and would have reported a healthy running server as deleted
out-of-band. The one kind it did recognise, `oci-instance`, has a single
resource to its name.

A NAME RULE CANNOT FIX THIS, and the fix is not a longer name rule:

  * `oci-apache` and `oci-kafka` are single compute instances and say nothing
    about it in their names;
  * `oci-oke`'s module DOES create instances, but the resource this kind TRACKS
    is a cluster — asking the compute API for a cluster's name finds nothing,
    which is the same false "missing" in the other direction;
  * reading the Terraform is no better: the legacy flat module declares a
    bucket, a database AND an instance.

So the question goes to the recipe. A blueprint that boots a machine already
has to declare HOW that machine reports on itself at first boot — a declaration
every manifest makes and test_every_machine_reports.py already enforces. It is
the existing authority on "is this a machine", not a new one.

Nothing here reaches OCI.
"""

from __future__ import annotations

import pytest

from orchestrator import cloud_state


#: What each kind actually is, established from the Terraform each blueprint
#: runs and from what the kind TRACKS — not from its name.
KINDS = [
    ("oci-service-vm", True, "oci_core_instance, and 24 of them exist"),
    ("oci-apache", True, "oci_core_instance behind a name that never says so"),
    ("oci-instance", True, "the one kind the old rule recognised"),
    ("oci-kafka", True, "oci_core_instance, also silent about it"),
    ("oci-oke", False, "its module makes instances; the kind tracks a CLUSTER"),
    ("oci-bucket", False, "object storage"),
    ("oci-postgres", False, "a managed database service"),
    ("aws-bucket", False, "not even the same cloud"),
]


@pytest.mark.parametrize("kind,is_machine,why", KINDS)
def test_every_resource_kind_is_classified_correctly(kind, is_machine, why):
    assert cloud_state._is_compute(kind) is is_machine, why


def test_the_kinds_the_old_name_rule_could_not_see():
    """THE defect, named. These three were false and had to become true; the
    old rule got them wrong for a reason no longer name can repair."""
    def old_name_rule(k):
        k = (k or "").lower()
        return "instance" in k or "compute" in k or k in ("vm", "oci-vm")

    for kind in ("oci-service-vm", "oci-apache", "oci-kafka"):
        assert old_name_rule(kind) is False, "the premise of this test changed"
        assert cloud_state._is_compute(kind) is True, kind


def test_a_cluster_is_not_a_machine_even_though_its_module_boots_one():
    """The other direction, and the one a Terraform scan would get wrong. OKE
    declares `boot_report: none` explicitly, which is the recipe saying that the
    thing this kind tracks does not boot and cannot report."""
    assert cloud_state._is_compute("oci-oke") is False


def test_a_kind_no_blueprint_claims_falls_back_to_the_old_rule():
    """A resource from outside the registry must be judged no worse than it was
    before, not silently reclassified."""
    assert cloud_state._is_compute("some-vendor-compute-thing") is True
    assert cloud_state._is_compute("some-vendor-storage-thing") is False


def test_a_registry_that_cannot_answer_is_not_a_verdict(monkeypatch):
    """Live cloud state runs against a mounted store that may be missing or
    unreadable. Failing to load a manifest must fall back, never raise into a
    reconcile that then reports an estate as gone."""
    from orchestrator import blueprint_registry

    def explode(kind):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(blueprint_registry, "for_resource_kind", explode)

    assert cloud_state._is_compute("oci-instance") is True
    assert cloud_state._is_compute("oci-bucket") is False


def test_every_shipped_blueprint_answers_the_question():
    """A new blueprint that forgets `boot_report` is silently 'not a machine',
    and its resources become invisible exactly as service-vm was. This holds the
    registry to answering for every kind it ships."""
    from orchestrator import blueprint_registry

    machines = {"oci-service-vm", "oci-apache", "oci-instance", "oci-kafka"}
    for manifest in blueprint_registry.discover():
        kind = manifest.get("resource_kind")
        if not kind:
            continue
        declared = str(manifest.get("boot_report") or "").strip().lower()
        assert cloud_state._is_compute(kind) is (kind in machines), (
            f"{kind} declares boot_report={declared!r} and is classified "
            f"{cloud_state._is_compute(kind)}")
