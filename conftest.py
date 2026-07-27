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
