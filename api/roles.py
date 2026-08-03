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

from db.session import SessionLocal

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
    "manage_access": {PLATFORM_ADMIN},    # F-IAM-01: edit the group->role map
    "manage_settings": {PLATFORM_ADMIN},  # F-OPS-09: edit runtime governance settings
}

_cache: dict[str, tuple[float, set[str]]] = {}
_CACHE_TTL = 300.0  # seconds — avoid calling Jira on every click


def role_source() -> str:
    """Where a signed-in user's portal roles come from.

    - 'portal' : the portal's own person -> role table (Admin -> Access control).
                 Used here because Entra provides login only and Jira has no
                 group model the portal can read.
    - 'jira'   : Jira group membership, via the group -> role map.
    - 'mock'   : ROLE_MAP config; unmapped users get FULL access, so it is for
                 local development only and must not be used in a live portal.
    """
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


def _load_db_role_map() -> dict | None:
    """DB-managed group->role mappings (RoleMapping), or None if the table is empty
    or unreadable. Separated so it can be exercised/overridden in tests."""
    try:
        from sqlalchemy import select

        from db.models import RoleMapping
        with SessionLocal() as session:
            rows = session.scalars(select(RoleMapping)).all()
        return {r.jira_group: r.role for r in rows} if rows else None
    except Exception:  # noqa: BLE001 — a DB hiccup must never deny; fall back to env
        return None


def _group_role_map() -> dict:
    """Jira group name -> portal role. The DB map (admin-managed, F-IAM-01) takes
    precedence; the JIRA_ROLE_MAP env is the fallback/seed."""
    db = _load_db_role_map()
    if db:
        return {g: r for g, r in db.items() if r in ALL_ROLES}
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
    if role_source() == "portal":
        # Authenticated but unlisted. They passed Entra, and raising a request is
        # harmless because Jira still approves it — so `requester`, never admin.
        return {REQUESTER}
    return set(ALL_ROLES) if role_source() != "jira" else {READ_ONLY}


def bootstrap_admins() -> set[str]:
    """Emails always treated as platform_admin, from PORTAL_BOOTSTRAP_ADMINS.

    Solves the chicken-and-egg of the portal role source: with an empty table
    nobody could reach the Admin console to grant the first role. Also the
    break-glass if the table is ever emptied. Deploy-time only (.env), never
    editable from the console — it is the one thing that can restore access.
    """
    raw = os.getenv("PORTAL_BOOTSTRAP_ADMINS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _portal_roles(email: str) -> set[str]:
    """Roles granted to this person in the portal's own table. Returns an empty
    set if unlisted; on a DB error returns empty so the caller falls back to the
    default rather than granting anything."""
    try:
        from sqlalchemy import select

        from db.models import UserRole
        session = SessionLocal()
        try:
            rows = session.scalars(
                select(UserRole).where(UserRole.email == email.strip().lower())
            ).all()
            return {r.role for r in rows if r.role in ALL_ROLES}
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — never grant on an error path
        return set()


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

    if role_source() == "portal":
        # Bootstrap admins are unconditional, so access can always be restored.
        if email.strip().lower() in bootstrap_admins():
            return {PLATFORM_ADMIN}
        return _portal_roles(email) or _default_roles()

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


def resolve_detail(email: str) -> dict:
    """What a given user resolves to right now: their directory groups and the
    roles those groups grant (F-IAM-01). Lets an admin verify the group->role map
    before turning live enforcement on."""
    return {
        "email": email,
        "source": role_source(),
        "groups": user_groups(email),
        "roles": sorted(resolve_roles(email)),
    }


def can(roles: set[str], action: str) -> bool:
    """True if any of the user's roles may perform the guarded action."""
    return bool(roles & ACTIONS.get(action, set()))


def clear_cache() -> None:
    _cache.clear()
