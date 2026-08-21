"""What the cloud offers that the catalogue does not — and what it no longer will.

WHY THIS EXISTS. The portal sells `postgres16`. The CLOUD decides what it will
still build, and it changes that without telling anyone. On 2026-08-17 that gap
cost four failed requests: the OKE module pinned Kubernetes v1.29.1, OCI had
retired it, and nothing in the portal knew until a real request failed at apply.
Somebody had to notice. Nobody did.

TWO DIRECTIONS, AND THE DANGEROUS ONE IS NOT THE OBVIOUS ONE.

  RETIRED — the catalogue sells a version the cloud no longer offers. This is a
    promise the portal cannot keep, and a user finds out by having their request
    fail after it was approved. This is the v1.29.1 case.

  NEWER — the cloud offers versions newer than anything the catalogue sells.
    Nobody is harmed, but the catalogue quietly falls behind: OCI offers
    PostgreSQL 13-18 and the portal sells only 16.

An OLDER version the cloud offers and the catalogue does not is NOT a gap. Not
selling PostgreSQL 13 is a decision, not an oversight, and reporting it as a
finding would bury the two that matter.

HONEST LIMIT. This can only compare families the cloud publishes an API for —
PostgreSQL versions and Kubernetes versions. Node, Java and Python versions come
from OS module streams measured on a real machine, not from an OCI endpoint, and
genuinely new SERVICES cannot be discovered at all: OCI has no "list everything
you sell" API. Most real churn is versions, which is what this covers.
"""

from __future__ import annotations

import re

# technology_code -> the family the cloud reports it under. Only families with a
# real cloud API are here; see the honest limit above.
_CODE_FAMILY = re.compile(r"^(postgres)(\d+)$")

FAMILY_LABEL = {"postgres": "PostgreSQL", "kubernetes": "Kubernetes"}


def parse_code(code: str) -> tuple[str, str] | None:
    """('postgres16') -> ('postgres', '16'), or None if the code carries no
    version this module can compare."""
    match = _CODE_FAMILY.match((code or "").strip().lower())
    return (match.group(1), match.group(2)) if match else None


def version_key(version: str) -> tuple:
    """Sortable form, so 18 beats 9 and v1.33.10 beats v1.33.1.

    String order would put "9" after "18" and "v1.33.1" after "v1.33.10" — the
    exact mistake that picked Oracle Linux 7 out of a list of 120 images.
    """
    parts = re.split(r"[.\-_]", (version or "").strip().lstrip("vV"))
    out: list[int] = []
    for part in parts:
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def compare(family: str, offered: list[str] | None,
            sold: list[str]) -> dict | None:
    """One family's gap, or None when there is nothing worth saying.

    `offered is None` means the cloud could not be asked. That is silence, not
    "it offers nothing", and must never be reported as everything being retired —
    a network blip would otherwise withdraw the entire catalogue.
    """
    if offered is None or not sold:
        return None

    offered_set = {str(v).strip() for v in offered if str(v).strip()}
    sold_set = {str(v).strip() for v in sold if str(v).strip()}

    retired = sorted(sold_set - offered_set, key=version_key)
    newest_sold = max(sold_set, key=version_key)
    newer = sorted((v for v in offered_set - sold_set
                    if version_key(v) > version_key(newest_sold)), key=version_key)

    if not retired and not newer:
        return None

    label = FAMILY_LABEL.get(family, family)
    notes = []
    if retired:
        notes.append(f"the catalogue sells {label} {', '.join(retired)}, "
                     f"which the cloud no longer offers")
    if newer:
        notes.append(f"the cloud offers {label} {', '.join(newer)}, "
                     f"newer than anything the catalogue sells")
    return {
        "family": family,
        "label": label,
        "sold": sorted(sold_set, key=version_key),
        "offered": sorted(offered_set, key=version_key),
        "retired": retired,
        "newer": newer,
        # A retirement is a promise the portal cannot keep; falling behind is
        # only an opportunity. They should not read as the same finding.
        "severity": "retired" if retired else "behind",
        "note": "; ".join(notes),
    }


def find_gaps(offered: dict[str, list[str] | None],
              technology_codes: list[str],
              pinned_kubernetes: str = "") -> list[dict]:
    """Every gap worth a human's attention, most serious first.

    `pinned_kubernetes` is handled separately because OKE carries no version in
    its technology code — the version is configuration, resolved from OCI at
    build time unless someone pins one. An unpinned portal always asks for a
    version the cloud offers and cannot be stale; a pinned one is exactly how
    REQ-2026-0148 failed.
    """
    gaps: list[dict] = []

    sold_by_family: dict[str, list[str]] = {}
    for code in technology_codes:
        parsed = parse_code(code)
        if parsed:
            sold_by_family.setdefault(parsed[0], []).append(parsed[1])

    for family, versions in sold_by_family.items():
        gap = compare(family, offered.get(family), versions)
        if gap:
            gaps.append(gap)

    pinned = (pinned_kubernetes or "").strip()
    if pinned:
        gap = compare("kubernetes", offered.get("kubernetes"), [pinned])
        if gap and gap["retired"]:
            gap["note"] = (f"OCI_OKE_KUBERNETES_VERSION is pinned to {pinned}, "
                           f"which OCI no longer offers. Every cluster request "
                           f"will fail at apply until this is changed or unset.")
            gap["severity"] = "retired"
            gaps.append(gap)

    gaps.sort(key=lambda g: (g["severity"] != "retired", g["family"]))
    return gaps
