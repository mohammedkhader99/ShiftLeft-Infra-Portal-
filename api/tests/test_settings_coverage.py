"""Guard: every portal setting must surface in the Admin console.

Standing rule (user, 2026-08-02): any configuration entry introduced in the
portal must automatically become part of the Admin console — editable or
non-editable according to policy and security.

This exists because the rule was broken silently: twelve settings added across
GAP-ANALYSIS steps 2–5 never reached the console, so a platform admin could not
tell from the UI whether the portal could create a billable database. Relying on
whoever adds the next setting to remember is exactly what failed. This test scans
the code and fails if a setting is registered nowhere — turning the rule into
something the build enforces rather than something people intend.

Adding a setting therefore forces a DECISION, one of four:
  1. `settings.ALLOWLIST`          — editable from the console
  2. `settings.READ_ONLY_ENV`      — visible, deploy-time only
  3. execution gate                — read by the ORCHESTRATOR, shown via /posture
  4. `EXCLUDED`, below             — with a documented reason
"""

import re
from pathlib import Path

from api import settings
from api.main import _EXECUTION_GATE_LABELS

_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = sorted((_ROOT / "api").glob("*.py")) + sorted((_ROOT / "orchestrator").glob("*.py"))
_READ = re.compile(r'(?:os\.getenv|settings\.env)\(\s*"([A-Z_][A-Z0-9_]*)"')

# The orchestrator's posture keys are snake_case; map them to the env vars they
# report so the scan can see them as covered.
_GATE_ENV = {
    "PROVISION_MODE", "CLOUD_STATE_MODE", "OCI_ACTUATE_ENABLED", "OCI_PSQL_ENABLED",
    "DNS_MODE", "OCI_DNS_ENABLED", "CONFIG_ENABLED", "BACKUP_MODE", "RESTORE_MODE",
    "REDUCE_MODE", "REFRESH_MODE",
    # OKE: reported as oke_bastion_cidr_set (set-or-not, not the range itself),
    # oke_mapped_tiers (tier NAMES, not the OCIDs behind them — an admin needs to
    # know which tiers are buildable, not to read the topology off a status
    # page), plus the version and cluster type.
    #
    # OCI_OKE_VCN_CIDR is gone: the module no longer chooses an address range,
    # it is handed subnets the network team allocated.
    "OCI_OKE_BASTION_CIDR", "OCI_OKE_NETWORKS", "OCI_OKE_KUBERNETES_VERSION",
    "OCI_OKE_CLUSTER_TYPE",
    # Cloud option catalogue: reported as catalogue_mode /
    # catalogue_shapes_allowed (a count, not the list) /
    # catalogue_image_filter_set.
    "OCI_CATALOGUE_MODE", "OCI_SHAPE_ALLOWLIST", "OCI_IMAGE_FILTER",
    # Reported as boot_report_configured (set-or-not; the value is a credential).
    "OCI_BOOT_REPORT_PAR_URL",
    # Kafka: reported as kafka_source_set — the value is a
    # pre-authenticated Object Storage URL, so it is never displayed.
    "OCI_KAFKA_SOURCE_URL",
}

# Deliberately NOT in the console, each for a stated reason. Adding to this list
# should be a conscious choice, not a shortcut.
EXCLUDED: dict[str, str] = {
    # --- Secrets: never displayed anywhere, at any privilege level -------------
    "ANTHROPIC_API_KEY": "secret",
    "AUDIT_HMAC_KEY": "secret",
    "INTERNAL_API_KEY": "secret",
    "SLACK_SIGNING_SECRET": "secret",
    "VAULT_TOKEN": "secret",
    "WEBHOOK_SECRET": "secret",
    "OCI_FINGERPRINT": "secret (key fingerprint)",
    "OCI_PRIVATE_KEY_PATH": "path to a private key",
    # --- Cloud identifiers: infrastructure addressing, not portal policy -------
    "OCI_TENANCY_OCID": "cloud identifier",
    "OCI_USER_OCID": "cloud identifier",
    "OCI_COMPARTMENT_OCID": "cloud identifier",
    "OCI_REGION": "cloud identifier",
    "OCI_KMS_KEY_OCID": "cloud identifier",
    "OCI_COMPUTE_SUBNET_OCID": "cloud identifier",
    "OCI_COMPUTE_IMAGE_OCID": "cloud identifier",
    "OCI_COMPUTE_IMAGE_MAP": "cloud identifier map",
    "OCI_COMPUTE_COMPARTMENT_OCID": "cloud identifier",
    "OCI_COMPUTE_SHAPE": "cloud sizing detail",
    "OCI_COMPUTE_SSH_AUTHORIZED_KEY": "credential material",
    "OCI_COMPUTE_USER_DATA": "boot script content",
    "OCI_PSQL_SUBNET_OCID": "cloud identifier (surfaced as psql_configured)",
    "OCI_PSQL_ADMIN_SECRET_OCID": "vault secret reference",
    "OCI_PSQL_ADMIN_SECRET_VERSION": "vault secret detail",
    "OCI_PSQL_ADMIN_USERNAME": "database account detail",
    "OCI_PSQL_COMPARTMENT_OCID": "cloud identifier",
    "OCI_PSQL_VERSION": "cloud sizing detail",
    "OCI_PSQL_SHAPE": "cloud sizing detail",
    "OCI_PSQL_SHAPE_FAMILY": "cloud sizing detail",
    "OCI_PSQL_STORAGE_IOPS": "cloud sizing detail",
    "OCI_DNS_ZONE": "cloud identifier (surfaced as dns_zone_set)",
    "OCI_DNS_TTL": "record detail",
    "OCI_DNS_COMPARTMENT_OCID": "cloud identifier",
    "AWS_REGION": "cloud identifier",
    "AWS_KMS_KEY_ARN": "cloud identifier",
    # --- Integration wiring: addresses and field mappings ---------------------
    "API_URL": "service address",
    "OPA_URL": "service address",
    "ORCHESTRATOR_URL": "service address",
    "TF_STATE_DIR": "filesystem path",
    "VAULT_ADDR": "service address",
    "VAULT_UI_ADDR": "service address",
    "VAULT_NAMESPACE": "vault detail",
    "VAULT_CREDS_PATH": "vault path template",
    "VAULT_GRANT_POLICY": "vault policy name",
    "VAULT_VERIFY": "TLS verification (deploy detail)",
    "JIRA_BASE_URL": "service address",
    "JIRA_PROJECT_KEY": "Jira schema detail",
    "JIRA_ISSUE_TYPE": "Jira schema detail",
    "JIRA_RESOLVE_FIELDS": "Jira field mapping",
    "JIRA_EXTRA_FIELDS": "Jira field mapping",
    "JIRA_TEMPLATE_ISSUE": "Jira schema detail",
    "JIRA_SET_REPORTER": "Jira schema detail",
    "JIRA_SUBSIDIARY_FIELD": "Jira field mapping",
    "JIRA_ROLE_MAP": "superseded by the DB-backed group->role panel",
    "ROLE_MAP": "dev-only identity map",
    "GROUP_MAP": "dev-only identity map",
    "OIDC_TENANT_ID": "identity provider detail",
    "OIDC_CLIENT_ID": "identity provider detail",
    "CHATOPS_APPROVE_STATUS": "Jira workflow mapping",
    "CHATOPS_REJECT_STATUS": "Jira workflow mapping",
    "CHATOPS_SLACK_MAP": "identity mapping",
    "CONFIG_OS_FAMILY": "image detail (orchestrator)",
    "CONFIG_PACKAGE_MAP": "image detail (orchestrator)",
    # --- Internal tuning + demo hooks ----------------------------------------
    "POLL_INTERVAL_SECONDS": "internal tuning",
    "LEADER_LEASE_TTL_SECONDS": "internal tuning",
    "SUBSIDIARY_SYNC_INTERVAL_SECONDS": "internal tuning",
    "PROVISION_TTL_DAYS": "internal default",
    "DEPARTED_OWNERS": "operational list, managed elsewhere",
    "CLOUD_STATE_SIMULATE_MISSING": "demo/test hook",
    "USE_MOCK": "shown read-only already",
}


def _settings_in_code() -> set[str]:
    found: set[str] = set()
    for path in _SCANNED:
        if path.name.startswith("test_"):
            continue
        found |= set(_READ.findall(path.read_text(encoding="utf-8")))
    return found


def _registered() -> set[str]:
    return set(settings.ALLOWLIST) | set(settings.READ_ONLY_ENV) | _GATE_ENV


def test_every_setting_is_registered_or_explicitly_excluded():
    """The rule, enforced. A new setting must be classified — the failure message
    tells you the four options rather than just complaining."""
    unclassified = sorted(_settings_in_code() - _registered() - set(EXCLUDED))
    assert not unclassified, (
        "These settings are read from the environment but appear nowhere in the "
        "Admin console:\n  " + "\n  ".join(unclassified)
        + "\n\nEvery setting must be classified (standing rule). Add each to ONE of:"
          "\n  1. api/settings.ALLOWLIST        - editable from the console"
          "\n  2. api/settings.READ_ONLY_ENV    - visible, deploy-time only"
          "\n  3. the orchestrator's /posture   - if the ORCHESTRATOR reads it"
          "\n  4. EXCLUDED in this file         - with a documented reason"
    )


def test_exclusions_are_real_and_documented():
    """Stale exclusions hide drift: if a setting is gone, its excuse should go too.
    And every exclusion must carry a reason, so the list can't become a dumping
    ground of bare names."""
    in_code = _settings_in_code()
    stale = sorted(k for k in EXCLUDED if k not in in_code)
    assert not stale, f"EXCLUDED lists settings no longer read in code: {stale}"
    assert all(v.strip() for v in EXCLUDED.values()), "every exclusion needs a reason"


def test_no_secret_is_ever_registered_for_display():
    """A secret must never be displayable, editable or otherwise. This is the one
    classification the rule does not permit."""
    displayed = _registered()
    for name in displayed:
        assert not any(t in name for t in ("SECRET", "TOKEN", "PASSWORD", "_KEY", "PAT")), (
            f"{name} looks like a secret but is registered for display in the console"
        )


def test_jira_workflow_mapping_is_visible_but_never_editable():
    """The Jira status mapping is shown because a workflow rename silently breaks
    approval detection, and the console is where that should be noticeable.

    It must stay READ-ONLY. 'Which Jira status means approved' IS the provisioning
    gate: making it editable would let someone redefine what counts as an approval
    from a browser and — with autonomous apply mode — provision unapproved
    requests. That is a deploy-time decision, not a console toggle.
    """
    mapping = {"JIRA_APPROVED_STATUSES", "JIRA_REJECTED_STATUSES",
               "JIRA_INPROGRESS_STATUS", "JIRA_RESOLVED_STATUS"}
    assert mapping <= set(settings.READ_ONLY_ENV), "the workflow mapping must be visible"
    assert not (mapping & set(settings.ALLOWLIST)), (
        "the Jira workflow status mapping must NEVER be editable from the console — "
        "it defines what counts as an approval")


def test_execution_gate_labels_cover_every_reported_gate():
    """Each gate the orchestrator reports must have a human label, or the console
    would show a raw key."""
    reported = {"provision_mode", "cloud_state_mode", "actuate_enabled", "psql_enabled",
                "psql_configured", "dns_mode", "dns_enabled", "dns_zone_set",
                "config_enabled", "backup_mode", "restore_mode", "reduce_mode",
                "refresh_mode"}
    assert reported <= set(_EXECUTION_GATE_LABELS), (
        f"unlabelled gates: {sorted(reported - set(_EXECUTION_GATE_LABELS))}")


def test_every_reported_gate_is_actually_displayed():
    """Reporting a gate through /posture is not the same as showing it.

    The standing rule is that a setting must APPEAR in the console. Gates added
    for OKE were reported by the orchestrator and had no label, so they were
    fetched and then silently dropped — registered by the letter of the rule and
    invisible by its purpose. This reads the keys the orchestrator's posture
    actually returns and requires a label for each.
    """
    src = (_ROOT / "orchestrator" / "main.py").read_text(encoding="utf-8")
    body = src.split("async def posture", 1)[1].split("\ndef ", 1)[0]
    reported = set(re.findall(r'^\s{8}"([a-z0-9_]+)":', body, re.M))
    assert reported, "could not read the posture keys — has the endpoint moved?"
    missing = sorted(reported - set(_EXECUTION_GATE_LABELS))
    assert not missing, (
        "The orchestrator reports these gates but the console has no label for "
        f"them, so they are never shown:\n  {missing}\n\n"
        "Add each to _EXECUTION_GATE_LABELS in api/main.py."
    )
