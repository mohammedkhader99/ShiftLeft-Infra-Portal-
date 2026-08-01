"""Role-based access control (E1.1, F-IAM-01).

A user's roles are resolved **server-side** (authoritative, P2) and gate the
privileged actions. Roles are sourced from **Jira group membership** — the
customer's identity hub: the signed-in email is resolved to a Jira user (the
email is not the Jira username here), whose groups are mapped to portal roles
via JIRA_ROLE_MAP. A config/mock fallback (ROLE_MAP by email) keeps local dev
and tests independent of live Jira.

Fail-safe: if roles can't be resolved, the user gets read-only (least
privilege), so a directory/Jira outage never silently grants power.
"""

import json
import os
import time

import httpx

# The six roles from ARCHITECTURE.md F-IAM-01.
REQUESTER = "requester"
APPROVER = "approver"
PLATFORM_ADMIN = "platform_admin"
AUDITOR = "auditor"
FINOPS = "finops"
READ_ONLY = "read_only"
ALL_ROLES = {REQUESTER, APPROVER, PLATFORM_ADMIN, AUDITOR, FINOPS, READ_ONLY}

# Which roles may perform each guarded action. Everything not listed is a
# read-only view available to any authenticated user.
ACTIONS = {
    "create_request": {REQUESTER, PLATFORM_ADMIN},
    "execute": {PLATFORM_ADMIN},          # approve / apply / destroy / decommission
    "grant_waiver": {PLATFORM_ADMIN, APPROVER},   # F-GOV-02: document a policy exception
    "grant_access": {PLATFORM_ADMIN, APPROVER},   # F-IAM-07: grant time-bound JIT access
    "view_audit": {AUDITOR, PLATFORM_ADMIN, REQUESTER},
    "view_overview": {PLATFORM_ADMIN, AUDITOR, FINOPS},   # whole-estate dashboard
}

_cache: dict[str, tuple[float, set[str]]] = {}
_CACHE_TTL = 300.0  # seconds — avoid calling Jira on every click


def role_source() -> str:
    """'jira' resolves roles from live Jira groups; 'mock' uses ROLE_MAP config."""
    return os.getenv("ROLE_SOURCE", "mock").strip().lower()


def _mock_role_map() -> dict:
    """email -> [roles], as ROLE_MAP JSON (mock/dev source)."""
    raw = os.getenv("ROLE_MAP", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _group_role_map() -> dict:
    """Jira group name -> portal role, as JIRA_ROLE_MAP JSON (live source)."""
    raw = os.getenv("JIRA_ROLE_MAP", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _default_roles() -> set[str]:
    """Roles for a user with no explicit mapping. Mock/dev: full access so local
    work and tests aren't blocked. Live: the least-privilege floor."""
    return set(ALL_ROLES) if role_source() != "jira" else {READ_ONLY}


def _jira_groups(email: str) -> list[str]:
    """Resolve the signed-in email to a Jira user and return their group names.

    Handles the email != Jira-username gap by searching, then falling back to the
    email local-part (which matches the Jira user key convention here). Raises on
    any HTTP failure so the caller can apply the read-only fail-safe.
    """
    base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    headers = {"Authorization": f"Bearer {os.getenv('JIRA_PAT', '')}"}

    def _search(query: str) -> list[dict]:
        r = httpx.get(f"{base}/rest/api/2/user/search",
                      params={"username": query}, headers=headers, timeout=8.0)
        r.raise_for_status()
        return r.json()

    users = _search(email) or _search(email.split("@")[0])
    if not users:
        return []
    key = users[0].get("key") or users[0].get("name")
    r = httpx.get(f"{base}/rest/api/2/user",
                  params={"key": key, "expand": "groups"}, headers=headers, timeout=8.0)
    r.raise_for_status()
    items = r.json().get("groups", {}).get("items", [])
    return [g.get("name") for g in items if g.get("name")]


def user_groups(email: str) -> list[str]:
    """The user's directory groups (F-IAM-09). Live: the Jira groups (read-only
    fail-safe = [] on any error). Mock: a GROUP_MAP JSON (email -> [group names]).
    Used to let a member of an environment's owning group act on it."""
    if not email:
        return []
    if role_source() == "jira":
        try:
            return _jira_groups(email)
        except Exception:  # noqa: BLE001 — never let a directory hiccup grant/deny wrongly
            return []
    try:
        return json.loads(os.getenv("GROUP_MAP", "") or "{}").get(email, [])
    except (ValueError, TypeError, AttributeError):
        return []


def jira_username(email: str) -> str | None:
    """Resolve an email to its Jira user key/name (email != Jira username here).

    Reuses the same search as _jira_groups. Returns None on any failure or no
    match — used by four-eyes (F-GOV-08) to compare the requester to the Jira
    approver; the caller fails open on None.
    """
    base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    headers = {"Authorization": f"Bearer {os.getenv('JIRA_PAT', '')}"}
    try:
        for query in (email, email.split("@")[0]):
            r = httpx.get(f"{base}/rest/api/2/user/search",
                          params={"username": query}, headers=headers, timeout=8.0)
            r.raise_for_status()
            users = r.json()
            if users:
                return users[0].get("key") or users[0].get("name")
    except Exception:  # noqa: BLE001
        return None
    return None


def resolve_roles(email: str) -> set[str]:
    """The user's roles. Mock: from ROLE_MAP/default (read fresh, no cache).
    Live: mapped from Jira group membership (cached), read-only on any failure."""
    if not email:
        return {READ_ONLY}

    if role_source() != "jira":
        mapped = _mock_role_map().get(email)
        if mapped is None:
            return _default_roles()
        return {r for r in mapped if r in ALL_ROLES} or {READ_ONLY}

    now = time.time()
    hit = _cache.get(email)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        groups = _jira_groups(email)
    except Exception:  # noqa: BLE001 — fail-safe to least privilege
        return {READ_ONLY}
    gmap = _group_role_map()
    roles = {gmap[g] for g in groups if g in gmap and gmap[g] in ALL_ROLES} or {READ_ONLY}
    _cache[email] = (now, roles)
    return roles


def can(roles: set[str], action: str) -> bool:
    """True if any of the user's roles may perform the guarded action."""
    return bool(roles & ACTIONS.get(action, set()))


def clear_cache() -> None:
    _cache.clear()
