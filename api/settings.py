"""Runtime settings store (F-OPS-09 — editable governance posture).

A curated allow-list of NON-SECRET governance / FinOps / AI settings can be
overridden at runtime from the Admin console. Every read follows the precedence:

    DB override (the `setting` table)  ->  environment (.env)  ->  built-in default

`env()` is a drop-in for `os.getenv` that consults the DB override first, but ONLY
for allow-listed keys — so the many other `os.getenv` reads across the codebase
are untouched.

Hard boundary (CLAUDE.md): secrets and security/provisioning-critical switches
(AUTH_MODE, PROVISION_MODE, credentials, …) are deliberately NOT in the allow-list
and are never written to or read from the DB. They stay in .env/vault. The store
only ever holds non-secret policy knobs whose values are safe to display.
"""

from __future__ import annotations

import os
import time

from sqlalchemy import select

from db.models import Setting
from db.session import SessionLocal

# --- The editable allow-list --------------------------------------------------
# key -> {label, help, type, default, group, choices?}. `type` is bool|int|float|
# str|enum; the reader parses the string value. `default` mirrors the built-in
# default at each read site, shown when neither DB nor .env sets the key.
ALLOWLIST: dict[str, dict] = {
    # Governance
    "SOD_ENFORCED": {"label": "Enforce segregation of duties", "type": "bool", "default": "true",
                     "group": "Governance",
                     "help": "A user can't approve/act on a request they raised."},
    "FOUR_EYES_ENFORCED": {"label": "Enforce four-eyes approval", "type": "bool", "default": "true",
                           "group": "Governance",
                           "help": "The Jira approver must differ from the requester."},
    "PROJECT_EXPIRY_ENFORCED": {"label": "Block requests on expired projects",
                                "type": "bool", "default": "false", "group": "Governance",
                                "help": ("When on, a request naming a project past its "
                                         "expiry date is blocked; otherwise it warns. "
                                         "A DISABLED project is refused either way.")},
    "PROJECT_EXPIRY_WARN_DAYS": {"label": "Project expiry warning lead (days)",
                                 "type": "int", "default": "30", "min": 0, "max": 365,
                                 "group": "Governance",
                                 "help": "Warn this many days before a project expires."},
    "PROVISION_MAX_ATTEMPTS": {"label": "Max provisioning attempts", "type": "int",
                               "default": "3", "min": 1, "max": 20, "group": "Governance",
                               "help": ("Stop retrying a request after this many failed "
                                        "handoffs. A permanent failure otherwise retries "
                                        "every poll cycle indefinitely.")},
    "APPROVAL_QUORUM": {"label": "Approval quorum", "type": "int", "default": "1", "min": 1,
                        "group": "Governance",
                        "help": "Number of distinct approvers required before provisioning."},
    "APPROVAL_SLA_HOURS": {"label": "Approval SLA (hours)", "type": "float", "default": "24", "min": 0,
                           "group": "Governance",
                           "help": "Hours before an awaiting-approval request is 'breached'."},
    # Change window
    "CHANGE_WINDOW_ENABLED": {"label": "Change window enabled", "type": "bool", "default": "false",
                              "group": "Change window",
                              "help": "Approved requests provision only inside the window."},
    "CHANGE_WINDOW_DAYS": {"label": "Change window days", "type": "str", "default": "mon-fri",
                           "group": "Change window", "help": "e.g. mon-fri, or mon,wed,fri."},
    "CHANGE_WINDOW_START": {"label": "Change window start", "type": "str", "default": "08:00",
                            "group": "Change window", "help": "24h time, e.g. 08:00."},
    "CHANGE_WINDOW_END": {"label": "Change window end", "type": "str", "default": "18:00",
                          "group": "Change window", "help": "24h time, e.g. 18:00."},
    "CHANGE_WINDOW_TZ": {"label": "Change window timezone", "type": "str", "default": "UTC",
                         "group": "Change window", "help": "e.g. UTC, Asia/Dubai."},
    # FinOps
    "TTL_DAYS_NONPROD": {"label": "Non-prod lifetime (days)", "type": "int", "default": "30", "min": 1,
                         "group": "FinOps",
                         "help": "Default lifetime for non-prod environments (prod/DR exempt)."},
    "TTL_WARN_DAYS": {"label": "TTL warning lead (days)", "type": "int", "default": "7", "min": 0,
                      "group": "FinOps", "help": "Warn owners this many days before expiry."},
    "TTL_ENFORCE": {"label": "Auto-reclaim expired non-prod", "type": "bool", "default": "false",
                    "group": "FinOps",
                    "help": "When on, expired non-prod environments are auto-decommissioned."},
    "BUDGET_ENFORCE": {"label": "Block over-budget requests", "type": "bool", "default": "false",
                       "group": "FinOps",
                       "help": "When on, a request over its cost-centre budget is blocked (else warned)."},
    "BUDGET_WARN_PCT": {"label": "Budget warning threshold (%)", "type": "float", "default": "90",
                        "min": 0, "max": 100, "group": "FinOps",
                        "help": "Warn when projected committed spend reaches this % of budget."},
    "VARIANCE_ALERT_PCT": {"label": "Variance alert threshold (%)", "type": "float", "default": "15",
                           "min": 0, "group": "FinOps",
                           "help": "Alert when actual cost drifts this % from the estimate."},
    "QUOTA_ENFORCE": {"label": "Block over-quota requests", "type": "bool", "default": "false",
                      "group": "FinOps",
                      "help": "When on, a create over its project's environment quota is blocked (else warned)."},
    "TTL_DAYS_SANDBOX": {"label": "Sandbox lifetime (days)", "type": "int", "default": "3", "min": 1,
                         "group": "FinOps",
                         "help": "How long a throwaway sandbox environment lives before expiry."},
    "COST_DRIFT_THRESHOLD": {"label": "Cost drift threshold (%)", "type": "float", "default": "10",
                             "min": 0, "group": "FinOps",
                             "help": "Flag an environment whose cost has drifted this far from its estimate."},
    # Scheduled shutdown
    "SHUTDOWN_ENABLED": {"label": "Record scheduled shutdown", "type": "bool", "default": "false",
                         "group": "Scheduled shutdown",
                         "help": "Record off-hours pause/resume for non-prod (portal-side; stops no real resource)."},
    "SHUTDOWN_DAYS": {"label": "Business days", "type": "str", "default": "mon-fri",
                      "group": "Scheduled shutdown", "help": "e.g. mon-fri."},
    "SHUTDOWN_START": {"label": "Business hours start", "type": "str", "default": "08:00",
                       "group": "Scheduled shutdown", "help": "24h time; outside these hours is off-hours."},
    "SHUTDOWN_END": {"label": "Business hours end", "type": "str", "default": "20:00",
                     "group": "Scheduled shutdown", "help": "24h time."},
    "SHUTDOWN_TZ": {"label": "Business hours timezone", "type": "str", "default": "UTC",
                    "group": "Scheduled shutdown", "help": "e.g. Asia/Dubai."},
    # AI
    "AI_MODE": {"label": "AI mode", "type": "enum", "default": "mock", "choices": ["mock", "live"],
                "group": "AI",
                "help": "'mock' is the free offline path; 'live' calls Claude (needs ANTHROPIC_API_KEY in .env)."},
    "AI_MODEL": {"label": "AI model", "type": "str", "default": "claude-opus-5", "group": "AI",
                 "help": "The Claude model used in live mode, e.g. claude-opus-5 or claude-haiku-4-5."},
}

# Shown read-only in the Admin panel so admins can see the posture, but managed in
# .env only (secrets/security/provisioning switches). key -> label. Values that are
# secret are reported as presence only (handled at the endpoint), never echoed.
READ_ONLY_ENV: dict[str, str] = {
    "AUTH_MODE": "Sign-in mode",
    "USE_MOCK": "Master mock switch",
    "PROVISION_MODE": "Provisioning mode",
    "AUTO_PROVISION": "Auto-provision poller",
    "ROLE_SOURCE": "Role source",
    # The break-glass administrator list. Visible so it is never a hidden back
    # door, read-only because it is the one thing that can restore access if the
    # role table is emptied — editable, it could also be used to grant it.
    "PORTAL_BOOTSTRAP_ADMINS": "Break-glass administrators (.env only)",
    "JIRA_MODE": "Jira mode",
    "RATE_LIMIT_BACKEND": "Rate-limit backend",
    "IAC_SCAN_ENFORCE": "IaC scan enforcement (orchestrator)",
    # The hourly cloud-option cache. Deploy-time rather than editable: turning
    # it on starts a thread that calls a real cloud API on a schedule.
    "OCI_CATALOGUE_ENABLED": "Hourly cloud option refresh",
    "OCI_CATALOGUE_REFRESH_SECONDS": "Cloud option refresh interval (seconds)",
    # How long the API caches the orchestrator's blueprint capabilities — which
    # OS families each recipe can configure. Deploy-time: capabilities change
    # when the orchestrator is redeployed, not while it runs.
    "BLUEPRINT_CAPABILITY_TTL_SECONDS": "Blueprint capability cache TTL (seconds)",
    # Integration + background-worker switches: visible so the posture is complete,
    # but deploy-time decisions rather than policy knobs.
    "VAULT_MODE": "Credential delivery mode",
    "WEBHOOKS_ENABLED": "Outbound webhooks",
    "SUBSIDIARY_SYNC_ENABLED": "Subsidiary sync from Jira",
    "CLOUD_STATE_SYNC_ENABLED": "Background cloud-state sync",
    # Jira workflow status mapping — which Jira status carries which meaning.
    # Visible because a workflow RENAME in Jira silently breaks approval
    # detection, and the console is where that should be noticeable.
    #
    # Read-only on purpose, despite being non-secret: "which status means
    # approved" IS the approval gate. Editing it from a browser would let someone
    # redefine what counts as an approval — and with autonomous apply mode that
    # would provision unapproved requests. It stays a deploy-time decision.
    "JIRA_APPROVED_STATUSES": "Jira status meaning APPROVED (the provisioning gate)",
    "JIRA_REJECTED_STATUSES": "Jira status meaning rejected",
    "JIRA_INPROGRESS_STATUS": "Jira status set while provisioning",
    "JIRA_RESOLVED_STATUS": "Jira status set when finished",
}

_CACHE_TTL = 3.0  # seconds — brief, so panel edits show up fast without hammering the DB
_cache: dict = {"at": 0.0, "map": {}}


def _load_overrides() -> dict[str, str]:
    """All DB overrides as {key: value}, cached briefly. Any DB error (table not
    migrated yet, DB down) falls back to the last good map (or empty), so a
    settings read can never break a request — it just uses .env/defaults."""
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL:
        return _cache["map"]
    try:
        session = SessionLocal()
        try:
            rows = session.scalars(select(Setting)).all()
            mapping = {r.key: r.value for r in rows}
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — never let a settings lookup fail a request
        _cache["at"] = now
        return _cache.get("map", {})
    _cache["at"] = now
    _cache["map"] = mapping
    return mapping


def invalidate_cache() -> None:
    """Drop the cache so the next read reflects a just-saved change immediately."""
    _cache["at"] = 0.0


def env(key: str, default: str | None = None) -> str | None:
    """os.getenv, but a DB override for an allow-listed key takes precedence."""
    if key in ALLOWLIST:
        overrides = _load_overrides()
        val = overrides.get(key)
        if val is not None:
            return val
    return os.getenv(key, default)


# --- Validation + effective-value reporting (used by the Admin API) ----------

def source_of(key: str) -> str:
    """Where the current value comes from: 'database', 'env', or 'default'."""
    if key in ALLOWLIST and _load_overrides().get(key) is not None:
        return "database"
    return "env" if os.getenv(key) is not None else "default"


def effective(key: str) -> str | None:
    """The current effective value string for an allow-listed key."""
    meta = ALLOWLIST[key]
    return env(key, meta["default"])


def coerce(key: str, raw: str) -> str:
    """Validate a proposed value for an allow-listed key and return the canonical
    string to store. Raises ValueError with a plain message on bad input."""
    meta = ALLOWLIST[key]
    kind = meta["type"]
    raw = (raw if raw is not None else "").strip()
    if kind == "bool":
        if raw.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            raise ValueError("Value must be true or false.")
        return "true" if raw.lower() in ("1", "true", "yes", "on") else "false"
    if kind == "enum":
        if raw not in meta["choices"]:
            raise ValueError(f"Value must be one of: {', '.join(meta['choices'])}.")
        return raw
    if kind == "int":
        try:
            n = int(raw)
        except ValueError:
            raise ValueError("Value must be a whole number.")
        if "min" in meta and n < meta["min"]:
            raise ValueError(f"Value must be at least {meta['min']}.")
        if "max" in meta and n > meta["max"]:
            raise ValueError(f"Value must be at most {meta['max']}.")
        return str(n)
    if kind == "float":
        try:
            f = float(raw)
        except ValueError:
            raise ValueError("Value must be a number.")
        if "min" in meta and f < meta["min"]:
            raise ValueError(f"Value must be at least {meta['min']}.")
        if "max" in meta and f > meta["max"]:
            raise ValueError(f"Value must be at most {meta['max']}.")
        return str(f).rstrip("0").rstrip(".") if "." in str(f) else str(f)
    # str
    if not raw:
        raise ValueError("Value can't be empty.")
    if len(raw) > 200:
        raise ValueError("Value is too long.")
    return raw
