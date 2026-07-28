"""Pytest configuration.

Pin all adapter/auth modes to "mock" for the whole test run so the suite never
depends on a developer's local .env. (api.main calls load_dotenv(), which would
otherwise leak live settings — e.g. AUTH_MODE=live set for a browser test — into
the test process and break unrelated tests.)

This runs at collection time, before any test module imports api.main, and
load_dotenv() does not override already-set variables, so these win.
"""

import os

for _var in ("AUTH_MODE", "AZURE_PRICING_MODE", "OCI_PRICING_MODE", "JIRA_MODE"):
    os.environ[_var] = "mock"
os.environ["USE_MOCK"] = "true"

# Pin config that tests assert on, so a developer's .env (real Jira workflow
# names, extra fields, etc.) can't leak in via load_dotenv and break them.
os.environ["JIRA_APPROVED_STATUSES"] = "Approved,Done"
os.environ["JIRA_REJECTED_STATUSES"] = "Rejected,Cancelled"
os.environ["JIRA_SET_REPORTER"] = "true"
# Set (not pop) to empty: load_dotenv(override=False) won't touch a var that
# already exists, but WILL set one that's absent — so popping wouldn't help.
os.environ["JIRA_EXTRA_FIELDS"] = ""
os.environ["JIRA_TEMPLATE_ISSUE"] = ""
