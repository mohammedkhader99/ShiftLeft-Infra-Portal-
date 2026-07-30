"""Pytest configuration.

Pin all adapter/auth modes to "mock" for the whole test run so the suite never
depends on a developer's local .env. (api.main calls load_dotenv(), which would
otherwise leak live settings — e.g. AUTH_MODE=live set for a browser test — into
the test process and break unrelated tests.)

This runs at collection time, before any test module imports api.main, and
load_dotenv() does not override already-set variables, so these win.
"""

import os

for _var in ("AUTH_MODE", "AZURE_PRICING_MODE", "OCI_PRICING_MODE", "JIRA_MODE",
             "PROVISION_MODE"):
    os.environ[_var] = "mock"
os.environ["USE_MOCK"] = "true"

# Pin config that tests assert on, so a developer's .env (real Jira workflow
# names, extra fields, etc.) can't leak in via load_dotenv and break them.
os.environ["JIRA_APPROVED_STATUSES"] = "Approved,Done"
os.environ["JIRA_REJECTED_STATUSES"] = "Rejected,Cancelled"
os.environ["JIRA_INPROGRESS_STATUS"] = "In Progress"
os.environ["JIRA_RESOLVED_STATUS"] = "Resolved"
os.environ["JIRA_SET_REPORTER"] = "true"
# Set (not pop) to empty: load_dotenv(override=False) won't touch a var that
# already exists, but WILL set one that's absent — so popping wouldn't help.
os.environ["JIRA_EXTRA_FIELDS"] = ""
os.environ["JIRA_TEMPLATE_ISSUE"] = ""
os.environ["JIRA_RESOLVE_FIELDS"] = ""
# Keep the background poller OFF during tests (it must never start a thread that
# calls the real orchestrator/Jira). Tests drive _advance_request directly.
os.environ["AUTO_PROVISION"] = "false"
# RBAC (E1.1): resolve roles from the local map, not live Jira. Empty map means
# unmapped users get full access (mock default), so existing tests aren't
# blocked; role-specific tests set ROLE_MAP themselves.
os.environ["ROLE_SOURCE"] = "mock"
os.environ["ROLE_MAP"] = ""
# Segregation of duties (E1.2) is ON in production but OFF in the test baseline,
# so the existing approve/apply/destroy tests (one actor) are unaffected; the
# dedicated SoD tests turn it on explicitly.
os.environ["SOD_ENFORCED"] = "false"
# Four-eyes (F-GOV-08): pinned OFF in the baseline so the developer's .env (which
# may disable it) can't leak in; the four-eyes tests turn it on explicitly.
os.environ["FOUR_EYES_ENFORCED"] = "false"
# Change window (F-GOV-05): pinned OFF so provisioning tests aren't held; the
# change-window tests drive it explicitly (or monkeypatch change_window_status).
os.environ["CHANGE_WINDOW_ENABLED"] = "false"
# Approval quorum (F-GOV-06): pinned to 1 (single approver) so existing
# provisioning tests aren't held; the quorum tests set it explicitly.
os.environ["APPROVAL_QUORUM"] = "1"
# Environment TTL (F-FIN-07): pin enforcement OFF so a sweep never auto-destroys
# in tests; the enforcement test enables it explicitly.
os.environ["TTL_ENFORCE"] = "false"
# Budget guardrails (F-FIN-02): pin enforcement OFF so an over-budget submit
# warns rather than blocks; the enforcement test turns it on explicitly.
os.environ["BUDGET_ENFORCE"] = "false"
