"""Remove a refutation the portal itself invalidated.

WHEN THIS IS LEGITIMATE, AND WHEN IT IS NOT.

`RecipeRefutation` records that a real machine disproved a recipe, so no machine
has to disprove it twice. That memory is load-bearing: without it the portal
spends a machine re-learning that `dnf install backup` is not a package.

But a refutation is evidence about A MACHINE THAT WAS BUILT, and the portal is
what builds it. When a defect in the BUILDER is what made the machine fail, the
row says something true about that machine and false about the recipe -- and
because it is keyed on the recipe's fingerprint, an unchanged recipe can never
be retried, however thoroughly the builder is fixed.

That is exactly what happened on 2026-09-06. grafana, minio and gitea were each
refused by a real proof, and none of them was broken software:

  * the data volume was mounted `:Z` without `:U`, so an image running as a
    non-root user could not write its own directory (grafana, uid 472);
  * a container port was published onto the host 1:1, so gitea was handed the
    machine's own sshd port;
  * there was no way to give an image a command, so minio printed its usage
    screen and exited.

All three were fixed in the renderer (a96ab6a). grafana's and gitea's recipes
are byte-identical to the refuted ones, so nothing would ever have retried them.

WHAT THIS TOOL WILL NOT DO. It does not touch `certification_proof`: the proofs
are the history, they are what actually happened, and a tool that quietly edited
them would make the record of a bad night disappear along with the bad night. It
refuses to run without a reason, and it writes that reason to the append-only
audit log, because "somebody deleted the evidence" and "the evidence was
withdrawn, by this person, for this stated cause" must not look the same.

DRY RUN BY DEFAULT.

    python ops/withdraw_a_refutation.py --codes grafana,minio,gitea --why "..."
    python ops/withdraw_a_refutation.py --codes grafana,minio,gitea --why "..." --apply
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from api.audit import append_audit
from db.models import RecipeRefutation
from db.session import SessionLocal

ACTOR = "ops/withdraw_a_refutation.py"


def rows_for(session, codes: list[str]) -> list[RecipeRefutation]:
    """Every refutation held against any of `codes`, oldest first."""
    wanted = [c.strip().lower() for c in codes if c.strip()]
    if not wanted:
        return []
    return list(session.scalars(
        select(RecipeRefutation)
        .where(RecipeRefutation.technology_code.in_(wanted))
        .order_by(RecipeRefutation.id)))


def described(row: RecipeRefutation) -> dict:
    """What the audit entry records about one withdrawn row.

    The FINGERPRINT is kept in full: it is the only thing that identifies which
    recipe was forgiven, and a truncated one could not be matched against a
    future refutation of the same recipe.
    """
    return {
        "id": row.id,
        "technology_code": row.technology_code,
        "deployment_target": row.deployment_target,
        "fingerprint": row.fingerprint,
        "proof_reference": row.proof_reference or "",
        "refuted_at": row.refuted_at.isoformat() if row.refuted_at else "",
    }


def main() -> int:  # pragma: no cover - the half that writes to the database
    parser = argparse.ArgumentParser(description=__doc__)
    # BY ID, NOT BY TECHNOLOGY, whenever some of a technology's refutations are
    # sound. Withdrawing everything held against grafana would have taken #40
    # with it -- "grafana installs but runs nothing, and its image says it
    # listens on 3000" -- which is a true finding about the PACKAGE rung, is
    # what correctly pushed grafana on to the container rung, and would have
    # cost a machine to re-learn. Listing by code and removing by id is the
    # difference between forgiving a recipe and forgetting a night.
    parser.add_argument("--codes", required=True,
                        help="comma-separated technology codes to list")
    parser.add_argument("--ids", default="",
                        help="comma-separated refutation ids to remove; without "
                             "it, --apply removes every row listed")
    parser.add_argument("--why", required=True,
                        help="why this evidence no longer stands; goes to the audit log")
    parser.add_argument("--apply", action="store_true",
                        help="actually remove them (otherwise this only lists)")
    args = parser.parse_args()

    if len(args.why.strip()) < 30:
        print("Refusing: --why must actually explain. A one-word reason in an "
              "audit log is the same as no reason at all.")
        return 2

    codes = [c.strip().lower() for c in args.codes.split(",") if c.strip()]
    with SessionLocal() as session:
        rows = rows_for(session, codes)
        if not rows:
            print(f"Nothing held against {', '.join(codes)}.")
            return 0

        print(f"{len(rows)} refutation(s) held against {', '.join(codes)}:\n")
        for row in rows:
            print(f"  #{row.id:<4} {row.technology_code:<10} {row.deployment_target:<5} "
                  f"{row.fingerprint[:16]}  {row.proof_reference}")
            said = (row.detail or "").strip() if hasattr(row, "detail") else ""
            if said:
                print(f"        said: {said[:150]}")
        print()

        wanted = {int(i) for i in args.ids.replace(",", " ").split() if i.strip()}
        doomed = [r for r in rows if r.id in wanted] if wanted else list(rows)
        missing = wanted - {r.id for r in rows}
        if missing:
            print(f"Refusing: no refutation {sorted(missing)} is held against "
                  f"{', '.join(codes)}. An id that names nothing is an id that "
                  f"names something else.")
            return 2
        if not doomed:
            print("Nothing selected.")
            return 0

        print(f"Would remove {len(doomed)} of {len(rows)}: "
              f"{', '.join('#' + str(r.id) for r in doomed)}")
        if not args.apply:
            print("DRY RUN — nothing was removed. Re-run with --apply to remove them.")
            return 0

        withdrawn = [described(row) for row in doomed]
        for row in doomed:
            session.delete(row)
        append_audit(session, "recipe.refutation.withdrawn", actor=ACTOR,
                     detail={"withdrawn": withdrawn, "why": args.why.strip(),
                             "codes": codes})
        session.commit()

    print(f"Removed {len(withdrawn)} refutation(s), and recorded why in the audit log.")
    print("The proofs themselves were not touched: they are the history.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
