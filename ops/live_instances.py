"""Which machines in the compartment are OURS, and is any proof machine still alive?

WHY THIS EXISTS
---------------
A throwaway version of this script reported, every day for a week:

    PROOF machines still alive: ['proof-oci-compute-req-2026-0097-instance']

It selected on the NAME — anything containing "proof" — and on that basis the
machine was reported as our litter, investigated as our litter, and very nearly
terminated as our litter on 2026-09-05. It is not ours. Its
`Oracle-Tags.CreatedBy` is `default/atul.kumar@noqodi.com`, while this
deployment authenticates to OCI as `mohammed.khader@emaratechg.ae`.

Three coincidences made someone else's VM look like ours, and no one of them
was enough on its own:

  * it is NAMED like a proof machine — `proof-…-instance`;
  * it is TAGGED `managed_by=infra-portal`, because the other deployment runs
    this same portal code;
  * it carries `reference=REQ-2026-0097`, a reference THIS portal also issued —
    for a different Apache machine that was destroyed cleanly on 5 August.

A NAME IS NOT AN IDENTITY. The compartment is shared: of eleven live instances,
ten were created by that other principal and one by an OKE nodepool. This
deployment currently owns none of them, which is the correct result — our proof
machines are torn down, and that is exactly what a working portal looks like.

So ownership is decided by the credential that created the machine, which is
Oracle's own tag and cannot be spoofed by naming. Everything else is context.

NOTHING IS HIDDEN. Another deployment's machines are still listed, under their
own heading, because "this compartment is shared with someone else" is the fact
the throwaway version obscured and the fact an operator most needs.

READ-ONLY. It lists and describes. It never stops, terminates or modifies
anything.

    python ops/live_instances.py
"""

from __future__ import annotations

import os
import sys

#: Oracle stamps this on every resource with the principal that created it.
CREATED_BY = "CreatedBy"
ORACLE_TAGS = "Oracle-Tags"

#: A proof machine of ours tags the proof it belongs to. Names are decoration;
#: this is the claim the portal itself writes and the one worth trusting.
PROOF_PREFIX = "PROOF-"

DEAD = ("TERMINATED", "TERMINATING")


def created_by(instance) -> str:
    """The principal Oracle recorded as having created this instance."""
    tags = getattr(instance, "defined_tags", None) or {}
    return str((tags.get(ORACLE_TAGS) or {}).get(CREATED_BY) or "").replace(
        "default/", "")


def reference_of(instance) -> str:
    """The portal reference the instance carries, or "" if it carries none."""
    tags = getattr(instance, "freeform_tags", None) or {}
    return str(tags.get("reference") or "")


def classify(instances, ours: str) -> dict:
    """Split live instances into ours, other people's, and unattributed.

    Pure, so it can be tested without a cloud. `ours` is the principal this
    deployment authenticates as — asked of OCI, never assumed.

    A machine with no CreatedBy tag is NOT quietly counted as ours. Absent is
    not the same as mine, and this whole script exists because something that
    looked like ours was not.
    """
    live = [i for i in instances
            if (getattr(i, "lifecycle_state", "") or "").upper() not in DEAD]
    mine, theirs, unattributed = [], [], []
    for instance in live:
        who = created_by(instance)
        if not who:
            unattributed.append(instance)
        elif ours and who.lower() == ours.lower():
            mine.append(instance)
        else:
            theirs.append(instance)

    # THE QUESTION THIS SCRIPT IS ACTUALLY FOR: a proof builds a machine,
    # verifies it and destroys it. One still running is money leaking, and it
    # can only be ours -- another deployment's proof is another deployment's
    # problem, and mistaking one for the other is what went wrong before.
    strays = [i for i in mine
              if reference_of(i).upper().startswith(PROOF_PREFIX)]
    return {"mine": mine, "theirs": theirs,
            "unattributed": unattributed, "strays": strays}


def _line(instance) -> str:
    created = getattr(instance, "time_created", None)
    when = f"{created:%d %b %H:%M}" if created else "?"
    reference = reference_of(instance) or "-"
    return (f"  {when}  {(getattr(instance, 'lifecycle_state', '') or '?'):12} "
            f"{reference:34} {getattr(instance, 'display_name', '') or '?'}")


def main() -> int:  # pragma: no cover - the cloud-touching half
    import oci

    from orchestrator.cloud_state import _oci_config

    config = _oci_config()
    compartment = (os.getenv("OCI_COMPUTE_COMPARTMENT_OCID")
                   or os.getenv("OCI_COMPARTMENT_OCID"))
    if not compartment:
        print("no compartment configured", file=sys.stderr)
        return 2

    # WHO WE ARE, asked rather than assumed. The whole judgement rests on it.
    identity = oci.identity.IdentityClient(config)
    ours = identity.get_user(config["user"]).data.name
    print(f"this deployment authenticates as : {ours}")
    print(f"compartment                      : {compartment[:32]}...\n")

    compute = oci.core.ComputeClient(config)
    everything = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment).data
    out = classify(everything, ours)

    print(f"OURS ({len(out['mine'])})")
    print("\n".join(_line(i) for i in out["mine"]) or "  (none)")

    if out["theirs"]:
        others = sorted({created_by(i) for i in out["theirs"]})
        print(f"\nANOTHER DEPLOYMENT'S ({len(out['theirs'])}) — created by "
              f"{', '.join(others)}")
        print("  This compartment is shared. These are listed so that is "
              "visible, and\n  must not be stopped or terminated from here.")
        print("\n".join(_line(i) for i in out["theirs"]))

    if out["unattributed"]:
        print(f"\nNO CreatedBy TAG ({len(out['unattributed'])}) — ownership "
              f"unknown, treat as someone else's")
        print("\n".join(_line(i) for i in out["unattributed"]))

    print()
    if out["strays"]:
        print(f"PROOF MACHINES OF OURS STILL ALIVE: {len(out['strays'])} — "
              f"these are billing and should have been destroyed")
        print("\n".join(_line(i) for i in out["strays"]))
        return 1
    print("PROOF MACHINES OF OURS STILL ALIVE: none — every proof tore its "
          "machine down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
