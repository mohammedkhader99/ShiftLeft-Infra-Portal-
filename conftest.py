"""Pytest configuration.

Pin all adapter/auth modes to "mock" for the whole test run so the suite never
depends on a developer's local .env. (api.main calls load_dotenv(), which would
otherwise leak live settings — e.g. AUTH_MODE=live set for a browser test — into
the test process and break unrelated tests.)

This runs at collection time, before any test module imports api.main, and
load_dotenv() does not override already-set variables, so these win.
"""

import os

# CLOUD_STATE_MODE joined this list on 2026-08-25, the day it was first set to
# "live" in a real .env. Four cloud-state and actuation tests failed at once,
# asserting on a mock adapter while the live one raised for want of credentials
# — the exact leak the block above exists to prevent, missing one name because
# nobody had ever set it.
for _var in ("AUTH_MODE", "AZURE_PRICING_MODE", "OCI_PRICING_MODE", "JIRA_MODE",
             "PROVISION_MODE", "CLOUD_STATE_MODE"):
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
# Quota guardrails (F-FIN-08): pin enforcement OFF so an over-quota create warns
# rather than blocks; the enforcement test turns it on explicitly.
os.environ["QUOTA_ENFORCE"] = "false"
# Ownership orphans (F-LCM-10): pin the departed-owners list empty so a
# developer's .env can't flag environments in unrelated tests; the orphan tests
# set DEPARTED_OWNERS explicitly.
os.environ["DEPARTED_OWNERS"] = ""
# Application hardening (F-SEC-09): the per-client rate limit is read per-request,
# so pin it OFF in the baseline — otherwise the whole suite's requests (same
# 'testclient' host) could trip it and break unrelated tests. The rate-limit test
# sets a low value itself.
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"
# Agent-written blueprints (C5a): GENERATED_BLUEPRINT_DIR defaults to
# "/generated/blueprints" — a path INSIDE THE CONTAINER. Unpinned, a test run on
# Windows resolved that to C:\generated\ and really wrote there: an
# onprem/postgres16 draft and its Terraform, outside the repository, created by
# the suite on 2026-08-21. Two failures followed from it, and both were the same
# fault — the tests read a directory that production had written to, so the
# result depended on what a previous run had left on the machine.
#
# mkdtemp, not tmp_path: this must be set before any module reads it at import
# time, which is before a fixture could run.
import tempfile as _tempfile

os.environ["GENERATED_BLUEPRINT_DIR"] = _tempfile.mkdtemp(prefix="portal-generated-")
