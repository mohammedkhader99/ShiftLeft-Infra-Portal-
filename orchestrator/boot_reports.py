"""Reading back what a machine said about itself.

The report is WRITTEN by the machine through a write-only pre-authenticated
request, and READ here with the orchestrator's own OCI credentials. Those are
deliberately different paths: the VM can add its own report and nothing else,
while the orchestrator — which already holds credentials for Terraform — can read
every report without a PAR at all.

WHY THIS EXISTS. The portal inferred success from Terraform exiting zero, and
that inference was wrong four times in one week: a service-vm that installed
nothing, an Apache whose runcmd was silently discarded, a Redis 6 delivered as a
7, and an nginx that lost port 80 to httpd. Every one of them booted healthy and
was reported provisioned. Run Command answers the question directly but does not
exist on OCI's Ubuntu images, and a private VM cannot be reached inbound — so the
machine reports outward instead, which works on any operating system.
"""

from __future__ import annotations

import os


class BootReportUnavailable(RuntimeError):
    """No report could be read — not the same as a report saying something bad."""


def bucket() -> str:
    """The bucket reports land in, derived from the PAR the machines write to.

    Read from the PAR rather than configured separately, so the two cannot point
    at different buckets — a split that would leave the portal reading an empty
    bucket while machines happily wrote to another.
    """
    par = (os.getenv("OCI_BOOT_REPORT_PAR_URL", "") or "").strip()
    if "/b/" not in par:
        return ""
    return par.split("/b/", 1)[1].split("/", 1)[0]


def object_name(reference: str, resource_kind: str) -> str:
    kind = (resource_kind or "resource").strip() or "resource"
    return f"{(reference or '').strip()}-{kind}.txt"


def _client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci

    from orchestrator.cloud_state import _oci_config
    return oci.object_storage.ObjectStorageClient(_oci_config())


def fetch(reference: str, resource_kind: str, client=None) -> str:
    """The machine's self-report, or raise BootReportUnavailable.

    Absence is reported as an exception rather than an empty string because the
    two mean opposite things: a report that says "nginx=inactive" is a machine
    telling us it is broken, while no report at all is us not knowing. Conflating
    them is how a silent failure reads as a healthy one.
    """
    name = bucket()
    if not name:
        raise BootReportUnavailable(
            "OCI_BOOT_REPORT_PAR_URL is not set, so no machine has been asked to "
            "report and none can be read.")
    client = client or _client()
    try:
        namespace = client.get_namespace().data
        response = client.get_object(namespace, name,
                                     object_name(reference, resource_kind))
        return response.data.text
    except Exception as exc:  # noqa: BLE001 - SDK raises many types
        raise BootReportUnavailable(
            f"no report for {reference}/{resource_kind}: {exc}") from exc


def reports_for(reference: str, resource_kind: str, client=None) -> dict[str, str]:
    """Every report the machines of one resource have written, by object name.

    Listed by PREFIX rather than fetched by exact name, because one resource kind
    can be more than one machine: a Kafka node writes ...-oci-kafka-node1.txt, and
    a GET of the bare name would 404 forever while a healthy cluster sat there
    reporting perfectly well.

    Raises BootReportUnavailable only when the bucket itself cannot be reached.
    An empty dict means "nothing has reported YET", which is a different fact
    from "the bucket is unreadable" and the caller has to treat it differently.
    """
    name = bucket()
    if not name:
        raise BootReportUnavailable(
            "OCI_BOOT_REPORT_PAR_URL is not set, so no machine has been asked to "
            "report and none can be read.")
    client = client or _client()
    prefix = f"{(reference or '').strip()}-{(resource_kind or 'resource').strip()}"
    try:
        namespace = client.get_namespace().data
        listing = client.list_objects(namespace, name, prefix=prefix).data
    except Exception as exc:  # noqa: BLE001 - SDK raises many types
        raise BootReportUnavailable(f"could not list reports for {prefix}: {exc}") from exc

    out: dict[str, str] = {}
    for obj in getattr(listing, "objects", None) or []:
        try:
            out[obj.name] = client.get_object(namespace, name, obj.name).data.text
        except Exception as exc:  # noqa: BLE001
            raise BootReportUnavailable(
                f"listed {obj.name} but could not read it: {exc}") from exc
    return out


def verdict(report: str) -> dict:
    """Read a report and say plainly whether the machine is working.

    Deliberately strict about services: a package that installed but a unit that
    is not active is EXACTLY the shape of every silent success this project has
    had, so it counts as a failure rather than a warning.
    """
    problems: list[str] = []
    for line in (report or "").splitlines():
        line = line.strip()
        if line.endswith("NOT INSTALLED"):
            problems.append(line)
        elif "=" in line and line.split("=", 1)[0].isidentifier():
            key, value = line.split("=", 1)
            if value == "inactive" or value == "failed":
                problems.append(f"{key} is {value}")
            elif key.startswith("http_") and value not in ("200", "301", "302", "403"):
                problems.append(f"{key} returned {value or 'nothing'}")
            elif key.startswith("firewall_") and value != "open":
                # A service can be installed, running and answering on loopback
                # while the machine's own firewall rejects every other host —
                # REQ-2026-0136 exactly. A port nobody can reach is not a working
                # service, whatever the daemon says about itself.
                problems.append(
                    f"port {key.split('_', 1)[1]} is not open in the firewall, so "
                    f"nothing outside this machine can reach the service")
        elif line.startswith("nothing listening on"):
            problems.append(line)
        # A step of the first-boot script that did not work, in its own words.
        #
        # This used to look for the word FAILED, which meant `PORTAL: could not
        # open 80/tcp` — a real failure, on a real machine — read as perfectly
        # healthy and REQ-2026-0136 was marked provisioned. Failure lines now
        # carry one fixed marker so the verdict never has to match prose.
        #
        # The second clause reads reports from machines built BEFORE that marker
        # existed. Matching their prose is exactly what this change was meant to
        # stop doing, and it is still right here: those machines are already
        # running and cannot be asked again. Matching only FAILED left the one
        # real legacy report — the nginx machine that started all this — still
        # reading as fine, which is how this was noticed.
        elif "PORTAL FAILURE:" in line or (
                "PORTAL:" in line
                and any(phrase in line.lower()
                        for phrase in ("failed", "could not", "no install recipe"))):
            problems.append(line)
    return {"ok": not problems, "problems": problems}
