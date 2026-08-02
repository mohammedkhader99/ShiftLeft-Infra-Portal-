"""Real backup & restore for managed databases (F-LCM-06, GAP-ANALYSIS step 2).

Pins the gates, the SDK calls, and — most importantly — that a restore never
claims to be in-place or verified when it is neither. The OCI SDK is mocked so
the suite stays offline.

HONEST LIMIT: these tests prove the adapter's logic and its refusals. They do NOT
prove behaviour against a real OCI database — that needs database credentials and
would create billable systems. Treat live backup/restore as UNVERIFIED until it
has been exercised against a real tenancy.
"""

import sys
import types

import pytest

from orchestrator import backups

_ENV = ("BACKUP_MODE", "RESTORE_MODE", "OCI_PSQL_ENABLED", "OCI_TENANCY_OCID",
        "OCI_USER_OCID", "OCI_FINGERPRINT", "OCI_REGION", "OCI_COMPARTMENT_OCID",
        "OCI_PSQL_COMPARTMENT_OCID")

_TARGET = {"reference": "REQ-2026-0042", "resource_id": "ocid1.psqldbsystem.oc1..db"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def _live(monkeypatch, action="both"):
    if action in ("backup", "both"):
        monkeypatch.setenv("BACKUP_MODE", "live")
    if action in ("restore", "both"):
        monkeypatch.setenv("RESTORE_MODE", "live")
    monkeypatch.setenv("OCI_PSQL_ENABLED", "true")
    monkeypatch.setattr(backups.cloud_state, "_require_oci_creds", lambda: None)
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "ocid1.compartment.oc1..c")


def _fake_oci(monkeypatch, captured=None, backup_id="ocid1.psqlbackup.oc1..b",
              new_system_id="ocid1.psqldbsystem.oc1..new", raises=None):
    """A stand-in `oci` module exposing just the psql surface the adapter uses."""
    class _Data:
        def __init__(self, _id):
            self.id = _id

    class _Result:
        def __init__(self, _id):
            self.data = _Data(_id)

    class _Client:
        def create_backup(self, create_backup_details=None):
            if raises:
                raise raises
            if captured is not None:
                captured["backup"] = create_backup_details
            return _Result(backup_id)

        def create_db_system(self, create_db_system_details=None):
            if raises:
                raise raises
            if captured is not None:
                captured["restore"] = create_db_system_details
            return _Result(new_system_id)

    class _CreateBackupDetails:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _CreateDbSystemDetails:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _BackupSourceDetails:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    mod = types.ModuleType("oci")
    mod.psql = types.SimpleNamespace(
        PostgresqlClient=lambda cfg: _Client(),
        models=types.SimpleNamespace(
            CreateBackupDetails=_CreateBackupDetails,
            CreateDbSystemDetails=_CreateDbSystemDetails,
            BackupSourceDetails=_BackupSourceDetails,
        ),
    )
    monkeypatch.setitem(sys.modules, "oci", mod)
    monkeypatch.setattr(backups, "_psql_client", lambda: _Client())


# --- Mock mode is the default and touches nothing ----------------------------

def test_backup_mock_by_default():
    out = backups.create_backup(_TARGET, "nightly")
    assert out["mock"] is True and out["backup_id"] == ""
    assert "no snapshot" in out["summary"]


def test_restore_mock_by_default():
    out = backups.restore_to_new_system(_TARGET, "any", "new")
    assert out["mock"] is True and "nothing changed" in out["summary"]


# --- Gates -------------------------------------------------------------------

def test_live_backup_requires_the_postgres_opt_in(monkeypatch):
    monkeypatch.setenv("BACKUP_MODE", "live")
    monkeypatch.setattr(backups.cloud_state, "_require_oci_creds", lambda: None)
    with pytest.raises(backups.BackupError, match="disabled"):
        backups.create_backup(_TARGET, "nightly")


def test_live_backup_requires_credentials(monkeypatch):
    monkeypatch.setenv("BACKUP_MODE", "live")
    monkeypatch.setenv("OCI_PSQL_ENABLED", "true")

    def _no_creds():
        raise RuntimeError("OCI credentials are not configured")
    monkeypatch.setattr(backups.cloud_state, "_require_oci_creds", _no_creds)
    with pytest.raises(backups.BackupError, match="not configured"):
        backups.create_backup(_TARGET, "nightly")


def test_backup_without_a_db_system_id_refuses(monkeypatch):
    """An environment with no recorded database can't be backed up — say so
    rather than recording a backup that protects nothing."""
    _live(monkeypatch, "backup")
    _fake_oci(monkeypatch)
    with pytest.raises(backups.BackupError, match="nothing to back up"):
        backups.create_backup({"reference": "REQ-1"}, "nightly")


def test_restore_without_a_cloud_backup_id_refuses(monkeypatch):
    """A restore-point recorded before live backups existed has no cloud backup —
    refuse instead of silently doing nothing."""
    _live(monkeypatch, "restore")
    _fake_oci(monkeypatch)
    with pytest.raises(backups.BackupError, match="cannot be restored"):
        backups.restore_to_new_system(_TARGET, "", "new")


# --- Live backup -------------------------------------------------------------

def test_live_backup_calls_oci_and_returns_the_backup_id(monkeypatch):
    captured: dict = {}
    _live(monkeypatch, "backup")
    _fake_oci(monkeypatch, captured=captured)
    out = backups.create_backup(_TARGET, "before-upgrade", retention_days=14)
    assert out["mock"] is False
    assert out["backup_id"] == "ocid1.psqlbackup.oc1..b"
    sent = captured["backup"]
    assert sent.db_system_id == "ocid1.psqldbsystem.oc1..db"
    assert sent.display_name == "before-upgrade"
    assert sent.retention_period == 14


def test_backup_api_error_is_surfaced(monkeypatch):
    _live(monkeypatch, "backup")
    _fake_oci(monkeypatch, raises=RuntimeError("service limit exceeded"))
    with pytest.raises(backups.BackupError, match="refused to create the backup"):
        backups.create_backup(_TARGET, "nightly")


# --- Live restore: the honesty properties ------------------------------------

def test_live_restore_creates_a_new_system_from_the_backup(monkeypatch):
    captured: dict = {}
    _live(monkeypatch, "restore")
    _fake_oci(monkeypatch, captured=captured)
    out = backups.restore_to_new_system(_TARGET, "ocid1.psqlbackup.oc1..b", "egate-restored")
    assert out["new_db_system_id"] == "ocid1.psqldbsystem.oc1..new"
    sent = captured["restore"]
    assert sent.display_name == "egate-restored"
    assert sent.source.source_type == "BACKUP"
    assert sent.source.backup_id == "ocid1.psqlbackup.oc1..b"


def test_live_restore_never_claims_in_place_or_verified(monkeypatch):
    """The two claims that would mislead a requester: that the original database
    was rewound, and that the restore was verified. Neither is true."""
    _live(monkeypatch, "restore")
    _fake_oci(monkeypatch)
    out = backups.restore_to_new_system(_TARGET, "ocid1.psqlbackup.oc1..b", "x")
    assert out["in_place"] is False
    assert out["verified"] is False
    assert "NEW database system" in out["summary"]
    assert "repoint" in out["summary"].lower()
    assert "original database is unchanged" in out["summary"]


def test_restore_api_error_is_surfaced(monkeypatch):
    _live(monkeypatch, "restore")
    _fake_oci(monkeypatch, raises=RuntimeError("backup not found"))
    with pytest.raises(backups.BackupError, match="refused to restore"):
        backups.restore_to_new_system(_TARGET, "b", "x")
