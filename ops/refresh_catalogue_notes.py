"""Bring live catalogue notes back into line with the reviewed seed.

WHAT WENT WRONG. `batch_prove --offer` lists a technology the moment a machine
certifies it, and composes the note itself. It passed `blueprint.notes` -- a
whole certification SENTENCE -- into the slot formatted as "Proved by {…}", so
every note written by a batch reads

    Proved by Certified automatically by proof build PROOF-GITEA-…: built,
    verified healthy and destroyed.: the portal built it, …

doubled, ungrammatical, and long enough that String(300) then cut the image name
off the end -- memcached's stopped at "docker.io/library/memcache". The composer
is fixed (deee427) and db/seed.py now carries clean text, but `_upsert_by`
INSERTS ONLY: re-seeding will not correct a row that already exists. These rows
need an explicit update, and this is it.

THE NOTE IS NOT DECORATION. It is what a requester reads on the form when
choosing what to ask for, and what an approver reads when deciding. A truncated
image name is the one fact in it nobody can reconstruct.

WHAT IT WILL NOT DO, and why the dry run matters. db/seed.py is the reviewed
source of truth for reference data, but a live note may have been edited by a
person through the Admin console, and this tool cannot tell that apart from
machine-written text. So it changes only what a person has read first: every
difference is printed in full, before and after, and nothing is written without
--apply. Delivery models are reported but NEVER changed -- regrouping a
technology is a catalogue decision, not a typo fix.

    python ops/refresh_catalogue_notes.py --codes gitea,grafana
    python ops/refresh_catalogue_notes.py --codes gitea,grafana --apply
    python ops/refresh_catalogue_notes.py --all-drifted        # list every one
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from api.audit import append_audit
from db.models import TechnologyDelivery
from db.seed import DELIVERY
from db.session import SessionLocal

ACTOR = "ops/refresh_catalogue_notes.py"


def changes(live: dict, seed: dict, codes=None) -> list[dict]:
    """What would change, as {code, was, now, model_differs}. Pure.

    `live` and `seed` are both {code: (delivery_model, note)}. A code absent from
    either side is skipped rather than invented: the seed is the reviewed text,
    not an instruction to create rows nobody certified.
    """
    wanted = ([c.strip().lower() for c in codes if c and c.strip()]
              if codes else sorted(set(live) & set(seed)))
    out = []
    for code in wanted:
        if code not in live or code not in seed:
            continue
        live_model, live_note = live[code]
        seed_model, seed_note = seed[code]
        if (live_note or "") == (seed_note or ""):
            continue
        out.append({
            "code": code,
            "was": live_note or "",
            "now": seed_note or "",
            # Reported so a mismatch is visible, never acted on: regrouping a
            # technology changes where it appears on the form, which is a
            # catalogue decision and not this tool's to make.
            "model_differs": (live_model or "") != (seed_model or ""),
            "live_model": live_model or "",
            "seed_model": seed_model or "",
        })
    return out


def _live(session) -> dict:  # pragma: no cover - reads the database
    return {row.technology_code: (row.delivery_model, row.note)
            for row in session.scalars(select(TechnologyDelivery))}


def main() -> int:  # pragma: no cover - the half that writes to the database
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--codes", help="comma-separated technology codes")
    group.add_argument("--all-drifted", action="store_true",
                       help="every row whose note differs from the seed")
    parser.add_argument("--apply", action="store_true",
                        help="actually write them (otherwise this only lists)")
    args = parser.parse_args()

    with SessionLocal() as session:
        live = _live(session)
        codes = (None if args.all_drifted
                 else [c.strip().lower() for c in args.codes.split(",") if c.strip()])
        pending = changes(live, DELIVERY, codes)

        if not pending:
            print("Every note asked about already matches the seed.")
            return 0

        print(f"{len(pending)} note(s) differ from db/seed.py:\n")
        for change in pending:
            print(f"  {change['code']}")
            print(f"    was: {change['was']}")
            print(f"    now: {change['now']}")
            if change["model_differs"]:
                print(f"    NOTE: delivery model differs "
                      f"({change['live_model']} live, {change['seed_model']} in "
                      f"the seed). NOT changed -- regrouping is a catalogue "
                      f"decision; correct it deliberately if it is wrong.")
            print()

        if not args.apply:
            print("DRY RUN — nothing was written. Re-run with --apply.")
            return 0

        for change in pending:
            row = session.get(TechnologyDelivery, change["code"])
            if row is not None:
                row.note = change["now"]
        append_audit(session, "catalogue.notes.refreshed", actor=ACTOR,
                     detail={"codes": [c["code"] for c in pending],
                             "why": "Notes written by batch_prove before deee427 "
                                    "embedded a certification sentence in the "
                                    "proof-reference slot, which truncated the "
                                    "image name. Restored from the reviewed "
                                    "db/seed.py text.",
                             "changes": [{"code": c["code"], "was": c["was"][:300]}
                                         for c in pending]})
        session.commit()

    print(f"Rewrote {len(pending)} note(s), and recorded the old text in the audit log.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
