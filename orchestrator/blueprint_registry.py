"""Blueprint discovery (F-CAT-10).

Scans `orchestrator/blueprints/*.yaml` and reports the build recipes this
orchestrator ships. The directory IS the registry: adding a blueprint is adding a
manifest, not editing code in several places.

Each manifest also declares its own preconditions (`enable_flag`, `requires_env`)
rather than those being hard-coded, so the portal can distinguish:

  * ready        — certified and configured; a request will build it
  * not configured — certified, but a required setting is missing, which would
                     otherwise only surface as an apply-time failure

A malformed manifest is skipped and reported rather than crashing discovery — one
bad file must not hide every other blueprint.
"""

from __future__ import annotations

import os
from pathlib import Path

BLUEPRINT_DIR = Path(__file__).resolve().parent / "blueprints"

_REQUIRED_FIELDS = ("ref", "target", "resource_kind", "builds")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _readiness(manifest: dict) -> tuple[bool, list[str]]:
    """Whether this recipe's preconditions are met, and what's missing."""
    missing: list[str] = []
    flag = manifest.get("enable_flag")
    if flag and not _truthy(os.getenv(flag)):
        missing.append(flag)
    for var in manifest.get("requires_env") or []:
        if not os.getenv(var):
            missing.append(var)
    return (not missing), missing


def discover(directory: Path | None = None) -> list[dict]:
    """Every valid manifest in the blueprints directory, newest-safe and sorted.

    Never raises: discovery failing closed would make the portal believe the
    orchestrator ships nothing, which reads as 'no automation available'.
    """
    import yaml

    root = directory or BLUEPRINT_DIR
    found: list[dict] = []
    try:
        paths = sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml"))
    except OSError:
        return []

    for path in paths:
        try:
            manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — a broken file must not hide the rest
            found.append({"ref": path.stem, "error": "manifest could not be parsed",
                          "builds": [], "target": "", "resource_kind": ""})
            continue
        if not isinstance(manifest, dict):
            continue
        if any(not manifest.get(f) for f in _REQUIRED_FIELDS):
            found.append({"ref": manifest.get("ref") or path.stem,
                          "error": f"manifest is missing one of {', '.join(_REQUIRED_FIELDS)}",
                          "builds": [], "target": manifest.get("target", ""),
                          "resource_kind": manifest.get("resource_kind", "")})
            continue

        ready, missing = _readiness(manifest)
        found.append({
            "ref": str(manifest["ref"]),
            "target": str(manifest["target"]).strip().lower(),
            "resource_kind": str(manifest["resource_kind"]),
            "module": str(manifest.get("module") or "."),
            "version": str(manifest.get("version") or ""),
            "builds": [str(b) for b in manifest.get("builds") or []],
            "description": str(manifest.get("description") or ""),
            # Module-specific inputs the standard variable set doesn't carry.
            # Merged over it at provision time, so a recipe with unusual inputs
            # stays a data change rather than a code change.
            "vars": manifest.get("vars") if isinstance(manifest.get("vars"), dict) else {},
            "ready": ready,
            "missing_config": missing,
        })
    return found


def for_resource_kind(kind: str) -> dict | None:
    """The manifest that builds a given resource kind, if any."""
    for bp in discover():
        if bp.get("resource_kind") == kind:
            return bp
    return None
