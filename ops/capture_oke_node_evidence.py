"""Catch an OKE worker node's boot output before OKE reaps it.

WHY THIS EXISTS. A node pool that fails with "N nodes(s) register timeout" is
torn down by OKE — not by Terraform — roughly 22 minutes after the nodes launch.
The nodes are the only witnesses to why they never joined the cluster, and they
are deleted before anyone can log in. REQ-2026-0150 and REQ-2026-0151 both died
this way with no node-side evidence at all.

Console history is the answer: OCI captures a running instance's serial console
output on demand, and THE CAPTURED RECORD SURVIVES THE INSTANCE. So this polls
for new worker nodes, captures each one as soon as it is RUNNING, and writes the
output to disk. It does not need SSH, a key, or a bastion, so it still works when
the login path is broken — which is exactly the situation that motivated it.

Run it alongside a build. It exits once it has captured every node it saw.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

import oci

from orchestrator.cloud_state import _oci_config


def _compartment() -> str:
    return (os.getenv("OCI_COMPUTE_COMPARTMENT_OCID")
            or os.getenv("OCI_COMPARTMENT_OCID") or "")


def workers(compute, compartment: str, since: str) -> list:
    """Worker nodes born after `since`. OKE names them oke-<clusterhash>-..."""
    found = []
    for i in compute.list_instances(compartment_id=compartment).data:
        if i.lifecycle_state in ("TERMINATED", "TERMINATING"):
            continue
        if not (i.display_name or "").startswith("oke-"):
            continue
        if str(i.time_created) < since:
            continue
        found.append(i)
    return found


def capture(compute, instance, out_dir: pathlib.Path, log) -> bool:
    """Capture one instance's console output. True if written."""
    try:
        details = oci.core.models.CaptureConsoleHistoryDetails(
            instance_id=instance.id,
            display_name=f"diag-{instance.display_name[-12:]}")
        history = compute.capture_console_history(details).data
    except Exception as exc:  # noqa: BLE001 - a capture failure must not stop the others
        log(f"   capture request failed for {instance.display_name}: {exc}")
        return False

    # The capture runs asynchronously; the content is not readable until it
    # succeeds. A node only lives ~22 minutes, so this cannot wait forever.
    deadline = time.time() + 300
    while time.time() < deadline:
        state = compute.get_console_history(history.id).data.lifecycle_state
        if state == "SUCCEEDED":
            break
        if state == "FAILED":
            log(f"   capture FAILED for {instance.display_name}")
            return False
        time.sleep(10)
    else:
        log(f"   capture timed out for {instance.display_name}")
        return False

    try:
        content = compute.get_console_history_content(
            history.id, length=1024 * 1024).data.value or ""
    except Exception as exc:  # noqa: BLE001
        log(f"   could not read content for {instance.display_name}: {exc}")
        return False

    path = out_dir / f"{instance.display_name}.console.log"
    path.write_text(content, encoding="utf-8", errors="replace")
    log(f"   WROTE {path}  ({len(content)} bytes)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True,
                    help='Only nodes created after this, e.g. "2026-08-17 07:00"')
    ap.add_argument("--out", default="/tfstate/node-evidence")
    ap.add_argument("--minutes", type=float, default=45.0)
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript = out_dir / "capture.log"

    def log(line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
        print(f"{stamp} {line}", flush=True)

    compute = oci.core.ComputeClient(_oci_config())
    compartment = _compartment()
    if not compartment:
        log("no compartment configured")
        return 2

    log(f"watching for worker nodes created after {args.since}")
    captured: set[str] = set()
    deadline = time.time() + args.minutes * 60

    while time.time() < deadline:
        try:
            live = workers(compute, compartment, args.since)
        except Exception as exc:  # noqa: BLE001 - keep watching through a blip
            log(f"list failed: {exc}")
            time.sleep(20)
            continue

        for node in live:
            if node.id in captured:
                continue
            if node.lifecycle_state != "RUNNING":
                continue
            # Give cloud-init time to produce something worth reading. Capturing
            # the instant it turns RUNNING yields an almost empty console.
            age = time.time() - node.time_created.timestamp()
            if age < 180:
                continue
            log(f"capturing {node.display_name} (up {age/60:.1f} min)")
            if capture(compute, node, out_dir, log):
                captured.add(node.id)

        if captured and len(captured) >= len(live) and live:
            log(f"captured all {len(captured)} node(s) seen; done")
            return 0
        time.sleep(20)

    log(f"window closed; captured {len(captured)} node(s)")
    return 0 if captured else 1


if __name__ == "__main__":
    sys.exit(main())
