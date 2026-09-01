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


def answered_but_broken(value: str) -> bool:
    """Whether this status is a server saying it could not serve.

    THE ONLY THING AN HTTP PROBE CAN CONDEMN A PORT FOR. Three checks are made
    of every declared port and each is authoritative for a different question:

        ss -lnt          is anything listening?      <- presence
        firewall_<port>  can anyone else reach it?   <- reachability
        http_<port>      what did it say?            <- only for HTTP services

    `000` used to be a failure, and REQ-2026-0195 shows what that costs: a
    RabbitMQ with all four ports bound on the host, all four open in the
    firewall, the pinned image running and the data volume mounted, failed
    because AMQP on 5672, epmd on 4369 and clustering on 25672 do not speak
    HTTP. curl gets no HTTP conversation, reports 000, and a working broker is
    called broken.

    Absence is already caught — `nothing listening on <port>` is a failure in
    its own right — so 000 adds nothing there and lies everywhere else. What it
    can still prove is the nginx case: a server that answered 5xx is running and
    unable to serve, which no socket check would notice.

    This is the SECOND HALF of the fix made an hour earlier. The port WAIT was
    changed from curl to `ss` and the port CHECK was left curling — the same
    defect, in the pair of the thing that was fixed.
    """
    code = (value or "").strip()
    if len(code) != 3 or not code.isdigit():
        return False
    return code[0] == "5"


def serving_http(value: str) -> bool:
    """Whether this status code proves something is serving HTTP on that port.

    THAT IS ALL THIS CHECK CAN PROVE, and the honest boundary is between "an
    HTTP server answered" and "nothing did". REQ-2026-0185 installed HashiCorp
    Vault correctly — rpm present, unit active, `ss` showing LISTEN on 8200,
    firewall open — and was failed because the report curls `/` and Vault's API
    lives under `/v1/`, so a bare `GET /` is answered 400. A 400 is an HTTP
    server declining a request; only a server can send one. Refusing it called a
    healthy machine broken, which costs trust exactly as a missed failure does.

    5xx STAYS A FAILURE, deliberately. It also proves a server answered, but it
    additionally says the server could not serve — a running-but-broken app is
    worth catching and is not what an API root returning 400 is.

    Anything that is not a three-digit status — `000`, a curl error message, an
    empty string — means no HTTP conversation happened at all.
    """
    code = (value or "").strip()
    if len(code) != 3 or not code.isdigit():
        return False
    return code[0] in "234"


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
            elif key.startswith("http_") and answered_but_broken(value):
                problems.append(
                    f"{key} returned {value} — the service answered and said it "
                    f"could not serve")
            elif key.startswith("image_") and not value.startswith("match"):
                # THE IMAGE ACTUALLY ON THE MACHINE, against the one pinned. A
                # digest is the whole security argument for the container rung —
                # a tag can be repointed by its publisher after a proof passed —
                # and it was being reported and never read.
                problems.append(
                    f"{key.split('_', 1)[1]} is not running the image that was "
                    f"pinned and proved: {value}")
            elif key.startswith("version_") and value.startswith("MISSING"):
                # The version command's own binary is not on the machine. The
                # guard below catches "asked and answered nothing"; this catches
                # "there was nothing to ask". REQ-2026-0188 reported
                # `UNPROMISED (12)` for software that was never installed — the
                # LINE NUMBER of a `command not found` error, read as a version
                # and waved through.
                problems.append(
                    f"{key.split('_', 1)[1]} could not be asked its version: "
                    f"{value[len('MISSING '):].strip('()')} is not on the "
                    f"machine, so nothing here confirms the software is "
                    f"installed under the name the recipe uses")
            elif key.startswith("version_") and value == "UNPROMISED (none)":
                # ASKED, AND COULD NOT ANSWER. Distinct from both a kept promise
                # and a broken one: the version command ran and produced no
                # number at all, which usually means the binary is not on the
                # path under the name the recipe expects. Letting this pass
                # because "nothing was promised" would build a check that cannot
                # fail — the report would say the machine answered when it did
                # not.
                problems.append(
                    f"{key.split('_', 1)[1]} was asked what version it is and "
                    f"could not say — the version command produced no number, so "
                    f"nothing on this machine confirms the software is really "
                    f"there under the name the recipe uses")
            elif (key.startswith("version_")
                  and not value.startswith(("OK", "UNPROMISED", "MISSING"))):
                # The machine was asked what version it actually has and compared
                # it with what the catalogue name promised. UNPROMISED means it
                # was asked and answered but nothing was promised to compare
                # against — software from a rolling vendor repository — which is
                # a fact on the record, not a broken promise. "Installed" and "is
                # the thing we sold them" are different facts: Redis 6.2 was
                # installed, running, and answering PONG under an entry called
                # "Redis 7", and every check the portal had said healthy.
                problems.append(f"{key.split('_', 1)[1]} is the wrong version — {value}")
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
    problems += _ports_never_served(report)
    return {"ok": not problems, "problems": problems}


def _ports_never_served(report: str) -> list[str]:
    """An image that declares ports and serves none of them is a broken machine.

    REQ-2026-0256 built OpenSearch and was marked provisioned on this report:

        container_opensearch=running
        declares_opensearch=9200,9300,9600,9650
        listening_inside_opensearch=none
        opened_ports=9300
        LISTEN 0      4096         0.0.0.0:9300       0.0.0.0:*
        firewall_9300=open

    Nothing inside the container was listening on anything, and every check the
    verdict made was satisfied. THE CHECK THAT COULD BE FOOLED PASSED AND THE
    CHECK THAT KNEW THE TRUTH WAS NEVER CONSULTED: `ss -lnt` on the host saw
    0.0.0.0:9300 and reported a listener, but that is podman's published-port
    proxy, which exists whether or not anything inside the container ever binds.
    `listening_inside_` is taken inside the container's own network namespace,
    it said `none`, and no rule read it.

    That is also why `answered_but_broken` can afford to forgive an `http_ 000`:
    it says absence "is already caught -- `nothing listening on <port>` is a
    failure in its own right". For a container it was not caught, because the
    host-side line the report writes is about the proxy.

    THE RULE IS THE ONE THE MACHINE ITSELF APPLIED. The first-boot script waits
    until a declared port is listening and gives up after three minutes; that
    verdict was computed on the machine and thrown away. Deriving it here from
    the two lines the report does carry means old reports are judged by it too,
    with nothing new asked of machines that are already running.

    DECLARING NOTHING IS NOT A FAILURE. An image with no ExposedPorts makes no
    claim to check -- a batch or worker container is entitled to listen on
    nothing -- so only a stated claim can be broken. This is the same care that
    keeps RabbitMQ passing: it declares six ports and a default container binds
    three by design, so ONE declared port listening is enough here, exactly as
    it is on the machine.

    Prefix matching, NOT `key.isidentifier()`: catalogue codes contain hyphens,
    so `declares_oracle-db=...` is not an identifier and the branch above skips
    it entirely. `archive_oracle-db=failed` was read as healthy for that reason.
    """
    declared: dict[str, str] = {}
    listening: dict[str, str] = {}
    for line in (report or "").splitlines():
        line = line.strip()
        for prefix, into in (("declares_", declared),
                             ("listening_inside_", listening)):
            if line.startswith(prefix) and "=" in line:
                key, value = line.split("=", 1)
                into[key[len(prefix):]] = value.strip()

    problems = []
    for code, decl in sorted(declared.items()):
        if decl in ("", "none"):
            continue
        got = listening.get(code, "none")
        have = set() if got in ("", "none") else set(got.split(","))
        if have & set(decl.split(",")):
            continue
        inside = ("nothing inside the container is listening at all"
                  if not have else
                  f"the only port listening inside it is {got}")
        problems.append(
            f"{code} is not serving any port it declares: the image declares "
            f"{decl} and {inside}. The container is running, so the software "
            f"started and did not begin serving -- it either failed after "
            f"start-up or was still starting when the machine gave up "
            f"waiting. Its own log is in this report.")
    return problems
