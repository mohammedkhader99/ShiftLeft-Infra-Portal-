"""Backup and restore for managed databases (F-LCM-06).

Backing up protects data; restoring recovers it. Until now the portal recorded
both as metadata while nothing happened in the cloud — a backup you cannot
actually restore from is worse than no backup, because it invites false
confidence. This module performs them for real against OCI Database with
PostgreSQL.

**Restore creates a NEW database system.** OCI managed PostgreSQL has no in-place
rollback: restoring a backup stands up a separate system with its own endpoint,
leaving the original untouched. That is surfaced to the requester verbatim — the
application must be repointed — rather than implying the old database was rewound.

Modes:
  - mock (default): records the action, touches no cloud. Safe for demos + tests.
  - live (BACKUP_MODE / RESTORE_MODE = live): the real OCI API, using the SAME
    credentials the orchestrator already holds for Terraform. Gated: it also needs
    the managed-PostgreSQL opt-in, because a restore creates a billable system.

The OCI SDK is lazy-imported so mock mode and the offline test suite never need it.
"""

from __future__ import annotations

import os

from orchestrator import cloud_state


class BackupError(RuntimeError):
    """A backup/restore could not be performed (not configured, or the API failed)."""


def backup_mode() -> str:
    return os.getenv("BACKUP_MODE", "mock").strip().lower()


def restore_mode() -> str:
    return os.getenv("RESTORE_MODE", "mock").strip().lower()


def _psql_client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci
    return oci.psql.PostgresqlClient(cloud_state._oci_config())


def _require_live(action: str) -> None:
    """Live backup/restore needs the orchestrator's OCI credentials AND the
    managed-PostgreSQL opt-in — a restore creates a real, billable DB system."""
    try:
        cloud_state._require_oci_creds()
    except Exception as exc:  # noqa: BLE001 — re-raise in this module's vocabulary
        raise BackupError(str(exc)) from exc
    from orchestrator import provisioner
    if not provisioner.psql_enabled():
        raise BackupError(
            f"Live {action} for managed PostgreSQL is disabled. Set OCI_PSQL_ENABLED=true "
            "to allow it."
        )


def _db_system_id(target: dict) -> str:
    """The OCI DB system OCID for the environment being backed up/restored."""
    ocid = (target or {}).get("resource_id") or ""
    if not ocid:
        raise BackupError(
            "No database system OCID recorded for this environment, so there is "
            "nothing to back up. It may have been provisioned before managed "
            "PostgreSQL was enabled."
        )
    return ocid


def create_backup(target: dict, label: str, retention_days: int = 31) -> dict:
    """Take a real backup of the environment's managed database.

    Returns {backup_id, label, mock, ...}. Mock records the intent only.
    """
    if backup_mode() != "live":
        return {"backup_id": "", "label": label, "mock": True,
                "summary": f"Mock backup '{label}' recorded — no snapshot was taken."}

    _require_live("backup")
    db_id = _db_system_id(target)
    try:
        import oci
        client = _psql_client()
        details = oci.psql.models.CreateBackupDetails(
            db_system_id=db_id,
            display_name=label,
            compartment_id=(os.getenv("OCI_PSQL_COMPARTMENT_OCID")
                            or os.getenv("OCI_COMPARTMENT_OCID")),
            retention_period=int(retention_days),
        )
        created = client.create_backup(create_backup_details=details)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any SDK/API error cleanly
        raise BackupError(f"OCI refused to create the backup: {exc}") from exc

    data = getattr(created, "data", None)
    backup_id = getattr(data, "id", "") or ""
    return {
        "backup_id": backup_id,
        "label": label,
        "mock": False,
        "db_system_id": db_id,
        "summary": f"Backup '{label}' created ({backup_id or 'id pending'}).",
    }


def restore_to_new_system(target: dict, backup_id: str, new_display_name: str) -> dict:
    """Restore a backup by creating a NEW managed database system from it.

    OCI has no in-place restore: the original system is left untouched and a new
    one is created. The caller must tell the requester to repoint the application
    — this returns the new system's id so that can be surfaced.
    """
    if restore_mode() != "live":
        return {"restored": True, "verified": True, "mock": True,
                "summary": f"Mock-restored to backup '{backup_id or 'n/a'}' — nothing changed."}

    _require_live("restore")
    if not backup_id:
        raise BackupError(
            "This restore point has no cloud backup id, so it cannot be restored. "
            "It was recorded before live backups were enabled."
        )
    try:
        import oci
        client = _psql_client()
        details = oci.psql.models.CreateDbSystemDetails(
            display_name=new_display_name,
            compartment_id=(os.getenv("OCI_PSQL_COMPARTMENT_OCID")
                            or os.getenv("OCI_COMPARTMENT_OCID")),
            source=oci.psql.models.BackupSourceDetails(
                source_type="BACKUP", backup_id=backup_id),
        )
        created = client.create_db_system(create_db_system_details=details)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"OCI refused to restore from the backup: {exc}") from exc

    data = getattr(created, "data", None)
    new_id = getattr(data, "id", "") or ""
    return {
        "restored": True,
        # A new system is not "verified" as a replacement for the old one — the
        # requester must connect to it. Never claim verification we didn't do.
        "verified": False,
        "mock": False,
        "new_db_system_id": new_id,
        "in_place": False,
        "summary": (
            f"Restored backup into a NEW database system ({new_id or 'id pending'}). "
            "The original database is unchanged — repoint the application at the new "
            "endpoint, then retire whichever system you no longer need."
        ),
    }
