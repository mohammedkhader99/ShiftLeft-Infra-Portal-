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
# REGISTRY_MODE joined this list on 2026-09-11, having never been on it. Every
# other adapter was pinned here and the registry was not, so the suite reached a
# public container registry over the internet with an 8-second timeout per ask —
# `test_asking_creates_nothing` alone took 68.9s of an 18-minute run, and the
# same suite has taken 9 minutes, 18 minutes and 1h44m with an identical pass
# count, decided by the network rather than by anything under test.
#
# THE SAME BLIND SPOT AS THE TWO ABOVE IT. This block reads as though it covers
# everything; it covers what somebody thought of. CLOUD_STATE_MODE was missing
# until the day it was first set to live, DATABASE_URL until the day the
# database container stopped, and this until somebody timed the suite.
# EVERY MODE THE CODE READS, not the ones somebody remembered. This list held
# six names while the code read seventeen, and the gap was invisible: each
# missing one defaults to "mock" anyway, so nothing fails until the day a
# developer's .env sets that one to live.
#
# That day has happened twice. CLOUD_STATE_MODE joined on 2026-08-25, the first
# time it appeared in a real .env — four tests failed at once, asserting on a
# mock adapter while the live one raised for want of credentials.
# REGISTRY_MODE joined on 2026-09-11, and that one had no safe default to hide
# behind: it is `live` by design, because production must ask a real registry.
# So the suite had always reached a public container registry over the internet,
# 8 seconds per ask, and said nothing about it.
#
# test_the_suite_stays_offline.py now checks this list against what the code
# actually reads, so the next one cannot be missed quietly.
for _var in ("AUTH_MODE", "AWS_PRICING_MODE", "AZURE_PRICING_MODE",
             "BACKUP_MODE", "CLOUD_STATE_MODE", "DNS_MODE", "GCP_PRICING_MODE",
             "JIRA_MODE", "OCI_CATALOGUE_MODE", "OCI_CLUSTER_DISCOVERY_MODE",
             "OCI_PRICING_MODE", "PROVISION_MODE", "REDUCE_MODE",
             "REFRESH_MODE", "REGISTRY_MODE", "RESTORE_MODE", "VAULT_MODE"):
    os.environ[_var] = "mock"
os.environ["USE_MOCK"] = "true"

# THE SUITE OWNS ITS OWN DATABASE. Without this, db/session.py falls back to
# postgresql://...@localhost:5432 and the app's startup `create_all` tries to
# reach it on every TestClient — in a suite that overrides `get_session` with
# SQLite everywhere, so the connection is never actually used for anything.
#
# It "worked" only because something else on this machine happened to be
# listening on 5432 and rejected the credentials immediately. The day that
# container stopped, the refusal went from instant to two seconds per address
# family, and every TestClient startup began costing four seconds — turning a
# forty-second file into one that looked hung. Nothing about the tests changed;
# an unrelated service went away.
#
# In-memory SQLite makes the startup path succeed instantly and depend on
# nothing outside the process, which is what a test suite's own database should
# do. Individual tests still build their own engines; this is only for the
# module-level `engine` that `_ensure_tables` touches.
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

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

# ALL FOUR, not just the blueprint directory. Only that one was pinned, so
# GENERATED_MODULE_DIR and GENERATED_PROFILE_DIR still resolved to their
# defaults during the suite -- the same hole, in the same file, for three
# more variables. Now that the fallback is inside the REPOSITORY, an
# unpinned profile directory would make the suite read the five real
# agent-written recipes in generated/profiles/, which is precisely the
# "tests read a directory that production had written to" failure this
# pinning exists to prevent.
_generated = _tempfile.mkdtemp(prefix="portal-generated-")
os.environ["GENERATED_ROOT"] = _generated
os.environ["GENERATED_BLUEPRINT_DIR"] = _generated + "/blueprints"
os.environ["GENERATED_MODULE_DIR"] = _generated + "/terraform"
os.environ["GENERATED_PROFILE_DIR"] = _generated + "/profiles"
