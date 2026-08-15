"""What the orchestrator's blueprints can do, as the API sees it.

THE API CANNOT IMPORT THE ORCHESTRATOR. Its image contains api/, common/ and db/
and nothing else — by design, since the orchestrator is a separate service that
holds the cloud credentials (ARCHITECTURE §3/§4). An earlier version of this
lookup did `from orchestrator import blueprint_registry` inside a try/except,
which imports cleanly under pytest (the repo root is on the path) and raises
ModuleNotFoundError in the container. The except branch returned "no opinion",
so the OS-image filtering and the Apache-on-Ubuntu refusal both passed every test
and were INERT in production — the running portal accepted Apache on Ubuntu right
up until someone checked.

So capabilities are fetched over the same signed channel the API already uses to
list blueprints, and cached: the request form asks per component, and a network
hop per dropdown would be absurd.

WHEN CAPABILITIES ARE UNKNOWN, SAY SO. `families_for` returns None rather than an
empty set, and callers must treat None as "cannot judge" and surface it, not as
"nothing is restricted". Silence is how the last failure hid.
"""

from __future__ import annotations

import os
import threading
import time

# {technology_code: {os families}} plus when it was fetched. Guarded by a lock
# because several requests can miss the cache at once.
_cache: dict[str, set[str]] | None = None
_limits: dict[str, tuple[str, int]] = {}
_refusals: dict[tuple[str, str], str] = {}
_proven: dict[str, set[str]] = {}
_fetched_at: float = 0.0
_lock = threading.Lock()


def ttl_seconds() -> int:
    """Blueprint capabilities change when the orchestrator is redeployed, so a
    few minutes is ample and keeps a restart from serving stale answers for long."""
    try:
        return max(30, int(os.getenv("BLUEPRINT_CAPABILITY_TTL_SECONDS", "300")))
    except ValueError:
        return 300


def _build(shipped: list[dict]) -> dict[str, set[str]]:
    """Flatten the manifest list to {technology: families it can be built on}.

    A family a real machine DISPROVED is removed here rather than filtered later,
    so every caller — the dropdown, validate(), anything added next — inherits the
    refusal without having to know it exists. python312 on Oracle Linux delivers
    Python 3.9.25 under a catalogue entry called "Python 3.12"; offering that
    image is offering a machine we know will be wrong.
    """
    out: dict[str, set[str]] = {}
    for bp in shipped or []:
        families = {str(f).strip().lower() for f in (bp.get("os_families") or [])}
        refuted = bp.get("refuted") or {}
        for code in bp.get("builds") or []:
            code = str(code)
            disproven = {str(f).strip().lower() for f in (refuted.get(code) or {})}
            out.setdefault(code, set()).update(families - disproven)
    return out


def _build_refusals(shipped: list[dict]) -> dict[tuple[str, str], str]:
    """{(technology, family): why a machine proved this does not work}."""
    out: dict[tuple[str, str], str] = {}
    for bp in shipped or []:
        for code, families in (bp.get("refuted") or {}).items():
            for family, why in (families or {}).items():
                out[(str(code), str(family).strip().lower())] = str(why)
    return out


def _build_limits(shipped: list[dict]) -> dict[str, tuple[str, int]]:
    """{technology: (resource-name suffix, longest name the module accepts)}.

    The suffix is what the orchestrator appends when composing a resource name,
    derived the same way it derives it: the part of the resource kind after the
    first hyphen. Kept here so the API can work out, at REQUEST time, whether an
    environment name will still be readable in the built resource.
    """
    out: dict[str, tuple[str, int]] = {}
    for bp in shipped or []:
        limit = int(bp.get("name_max_length") or 0)
        if not limit:
            continue
        kind = str(bp.get("resource_kind") or "")
        suffix = kind.split("-", 1)[1] if "-" in kind else kind
        for code in bp.get("builds") or []:
            out[str(code)] = (suffix, limit)
    return out


def refresh(fetcher) -> bool:
    """Re-read capabilities from the orchestrator. True if the cache was updated.

    A failed fetch leaves the previous answer in place, for the same reason the
    cloud-option cache does: stale capability data is better than none, and none
    means the filter quietly stops filtering.
    """
    global _cache, _fetched_at
    try:
        shipped = fetcher()
    except Exception:  # noqa: BLE001 - a lookup must never break a page render
        return False
    if not shipped:
        return False
    with _lock:
        _cache = _build(shipped)
        _limits.clear()
        _limits.update(_build_limits(shipped))
        _refusals.clear()
        _refusals.update(_build_refusals(shipped))
        _proven.clear()
        for bp in shipped or []:
            for code, families in (bp.get("verified") or {}).items():
                _proven.setdefault(str(code), set()).update(
                    str(f).strip().lower() for f in families or [])
        _fetched_at = time.time()
    return True


def families_for(code: str, fetcher) -> set[str] | None:
    """OS families the blueprint that builds `code` declares, or None.

    None means one of two different things, and both must be surfaced rather
    than silently treated as "allow everything":
      * capabilities have never been fetched (the orchestrator is unreachable), or
      * no blueprint claims this technology at all.
    """
    global _cache
    if _cache is None or (time.time() - _fetched_at) > ttl_seconds():
        refresh(fetcher)
    if _cache is None:
        return None
    return _cache.get((code or "").strip())


def name_budget(code: str, reference_length: int, fetcher) -> tuple[int, str] | None:
    """(longest environment name that stays intact, blueprint suffix) for a
    technology, or None if the blueprint states no limit.

    The orchestrator composes <environment>-<reference>-<suffix> and, when that
    overruns, trims the ENVIRONMENT name — never the reference, which is what
    keeps the name unique. Trimming is lossless for the reference and lossy for
    the environment, so the portal warns about the second and stays quiet about
    the first.

    `reference_length` is the SHORTENED reference the orchestrator will use
    (REQ-2026-0128 -> 26-0128), because that is what actually consumes budget.
    """
    if _cache is None:
        refresh(fetcher)
    entry = _limits.get((code or "").strip())
    if not entry:
        return None
    suffix, limit = entry
    return limit - reference_length - len(suffix) - 2, suffix


def evidence(code: str) -> dict:
    """What a machine has proved about this technology, per OS family.

    Returns {"proven": {...}, "refuted": {...}, "known": bool}. `known` is False
    when capabilities have never been read — and a caller deciding whether to
    CERTIFY must treat that as "cannot judge", never as "nothing objected".
    """
    code = (code or "").strip()
    return {
        "proven": set(_proven.get(code, set())),
        "refuted": {f for (c, f) in _refusals if c == code},
        "known": _cache is not None,
    }


def refusal(code: str, family: str) -> str:
    """Why a machine proved this combination wrong, or "" if there is no such
    evidence. Distinct from "not supported": this one was BUILT and measured."""
    return _refusals.get(((code or "").strip(), (family or "").strip().lower()), "")


def known() -> bool:
    """Whether capabilities have ever been read. Surfaced in the admin console so
    an inert filter is visible instead of looking like a permissive one."""
    return _cache is not None


def reset() -> None:
    """Drop the cache (tests, and after a blueprint change)."""
    global _cache, _fetched_at
    with _lock:
        _cache, _fetched_at = None, 0.0
        _limits.clear()
        _refusals.clear()
        _proven.clear()
