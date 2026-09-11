"""Ask one question: can something inside the VCN reach the cluster's API? (K.1)

WHY THIS EXISTS AT ALL. Every note in this repository said "the orchestrator has
no route to the Kubernetes API endpoint", which is true, and was read for months
as "a network change is required", which is not. The OKE module's own security
group admits TCP 6443 from `operator_cidr`, and `operator_cidr` is
`data.oci_core_vcn.provided.cidr_block` — the WHOLE VCN. The orchestrator cannot
reach it because it runs in a container outside the VCN, not because no rule
permits it.

The portal already builds machines inside that VCN. So Phase K asks whether one
of them can reach the API, and this is the smallest honest way to find out.

IT ASKS ABOUT THE ROUTE, NOT ABOUT AUTHENTICATION. Reachability is a TCP and TLS
question. A `401 Unauthorized` from the API server is a COMPLETE SUCCESS here —
it means the packets arrived, the handshake completed and Kubernetes answered.
Asking it to authenticate as well would need an instance principal, an IAM
policy and a kubeconfig, and would confuse "we cannot get there" with "we got
there and were not allowed in". Those have entirely different remedies, and only
the first one decides whether Phase K is possible.

That is why K.1 needs no IAM policy, no OCI CLI and no kubectl. The plan said it
would; the plan was wrong in the direction of doing more than the question
requires. Authentication starts in K.2.

WHAT IT DOES NOT PROVE. The certificate is not verified — `curl -k` — so this
establishes a ROUTE and says nothing about trust. K.2 must verify properly
against the cluster's own CA, and a probe that passed here tells it nothing
about that.

NOTHING IS DECIDED HERE. The probe reports; `verdict` reads what it reported. A
missing report is never a pass.
"""

from __future__ import annotations

import shlex

#: The resource kind the probe blueprint claims. Its own kind rather than a flag
#: on another, so the existing plan/apply/destroy path carries it with no
#: dispatch code — see blueprint_registry: adding a recipe is adding a folder and
#: a manifest.
PROBE_KIND = "oci-oke-probe"

#: The port the Kubernetes API answers on, and the one the NSG rule names.
API_PORT = 6443

REACHABLE, UNREACHABLE, UNKNOWN = "reachable", "unreachable", "unknown"


def script(endpoint: str, report_url: str = "", *, timeout: int = 10) -> str:
    """Cloud-init that asks the question once and says what it found.

    `endpoint` is the cluster's private API address — host or host:port.
    `report_url` is a pre-authenticated, write-only object-storage URL, the same
    channel every other machine here reports through, which needs no inbound
    access to the VCN.
    """
    endpoint = (endpoint or "").strip()
    if not endpoint:
        raise ValueError("a probe needs an API endpoint to ask about")
    if ":" not in endpoint.rsplit("]", 1)[-1]:
        endpoint = f"{endpoint}:{API_PORT}"

    url = f"https://{endpoint}/version"
    lines = [
        "#!/bin/bash",
        # NOT `set -e`. A probe that exits on the first failure never writes the
        # report saying what failed, and a missing report is exactly what an
        # unreachable endpoint would produce — indistinguishable from a machine
        # that never booted.
        "set -u",
        "REPORT=/tmp/oke-probe.txt",
        "BODY=/tmp/oke-probe-body",
        "ERR=/tmp/oke-probe-err",
        "{",
        '  echo "probe=oke-api"',
        f'  echo "endpoint={endpoint}"',
        f"  CODE=$(curl -sk -o $BODY -m {int(timeout)} -w '%{{http_code}}' "
        f"{shlex.quote(url)} 2>$ERR)",
        '  [ -z "$CODE" ] && CODE=000',
        '  echo "http_status=$CODE"',
        # 000 is curl's "I never got an answer": DNS, routing, refused, timed
        # out. Anything else — including 401 and 403 — means the packets
        # arrived, TLS completed and Kubernetes replied, which is the whole
        # question.
        '  if [ "$CODE" = "000" ]; then',
        f'    echo "result={UNREACHABLE}"',
        '    echo "detail=$(head -c 200 $ERR | tr \'\\n\' \' \')"',
        "  else",
        f'    echo "result={REACHABLE}"',
        '    echo "detail=$(head -c 300 $BODY | tr \'\\n\' \' \')"',
        "  fi",
        "} > $REPORT",
    ]
    if report_url:
        # PUT whatever was found, including a failure. The report is the only
        # thing that comes back out, so not sending one turns every negative
        # answer into silence.
        lines.append(
            f"curl -s -X PUT --data-binary @$REPORT {shlex.quote(report_url)} "
            f">/dev/null 2>&1 || true")
    lines.append("")
    return "\n".join(lines)


def verdict(report: str | None) -> dict:
    """What the probe found, read from its own words.

    `unknown` is a distinct answer from `unreachable`: nothing reported at all
    means the machine may never have booted, and calling that "the API is
    unreachable" would blame the network for a build that never happened.
    """
    if not (report or "").strip():
        return {"result": UNKNOWN, "reason": "No probe report was written.",
                "http_status": None, "endpoint": None, "detail": None}

    fields: dict[str, str] = {}
    for line in report.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields.setdefault(key.strip(), value.strip())

    result = fields.get("result", "")
    status = fields.get("http_status") or None
    endpoint = fields.get("endpoint") or None
    detail = fields.get("detail") or None

    if result == REACHABLE:
        return {
            "result": REACHABLE, "http_status": status, "endpoint": endpoint,
            "detail": detail,
            "reason": (f"The API endpoint answered on port {API_PORT} "
                       f"(HTTP {status}). A machine inside the VCN can reach it; "
                       f"whether it may authenticate is a separate question."),
        }
    if result == UNREACHABLE:
        return {
            "result": UNREACHABLE, "http_status": status, "endpoint": endpoint,
            "detail": detail,
            "reason": (f"Nothing answered at {endpoint or 'the API endpoint'}. "
                       f"The route Phase K depends on does not exist as assumed, "
                       f"and the rest of the phase should not be built on it."),
        }
    return {
        "result": UNKNOWN, "http_status": status, "endpoint": endpoint,
        "detail": detail,
        "reason": ("The report does not say what happened, which is not the same "
                   "as saying the endpoint is unreachable."),
    }
