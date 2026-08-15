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


def _verified_for(builds: list) -> dict:
    """{technology: [families it has been booted and checked on]}."""
    try:
        from orchestrator.configure import VERIFIED
    except Exception:  # noqa: BLE001 - discovery must never fail closed
        return {}
    out: dict[str, list[str]] = {}
    for code, family in VERIFIED:
        if code in {str(b) for b in builds}:
            out.setdefault(code, []).append(family)
    return {k: sorted(v) for k, v in out.items()}


def _refuted_for(builds: list) -> dict:
    """{technology: {family: why}} for the codes this blueprint builds.

    Read from configure.REFUTED, which only ever records what a machine was built
    and asked. Imported lazily so a registry listing never depends on the recipe
    table being importable.
    """
    try:
        from orchestrator.configure import REFUTED
    except Exception:  # noqa: BLE001 - discovery must never fail closed
        return {}
    out: dict[str, dict[str, str]] = {}
    for (code, family), why in REFUTED.items():
        if code in {str(b) for b in builds}:
            out.setdefault(code, {})[family] = why
    return out


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
            # OS families this blueprint's own first-boot configuration can
            # handle. Empty means it configures no operating system at all — a
            # bucket, a managed database — and the portal makes no OS claim for
            # it. The portal refuses an image whose family is not listed here,
            # which is the only way it can know that Apache's Red Hat-only
            # cloud-init must not be pointed at an Ubuntu image.
            "os_families": [str(f).strip().lower()
                            for f in manifest.get("os_families") or []],
            # Which mechanism carries this blueprint's boot self-report:
            # `template` (its own cloud-init embeds it, so the module needs
            # boot_report_url as a variable), `user_data` (configure.py already
            # baked it in), or `none` (exempt, with the reason written down).
            # The provisioner reads this to decide whether to pass the URL.
            "boot_report": str(manifest.get("boot_report") or ""),
            # Combinations a real machine PROVED do not deliver what the
            # catalogue name promises. Published beside os_families because the
            # portal needs both to decide what to offer: one says what the recipe
            # can configure, the other says where it was caught lying.
            "refuted": _refuted_for(manifest.get("builds") or []),
            # Combinations a real machine PROVED work. The portal refuses to
            # certify anything it cannot see evidence for, so this is what makes
            # "certified" mean something rather than "somebody clicked".
            "verified": _verified_for(manifest.get("builds") or []),
            "description": str(manifest.get("description") or ""),
            # Which Terraform variable carries the environment's name. Modules
            # disagree (bucket_name, instance_name, db_name...), and hard-coding
            # the mapping meant a new recipe silently received an empty name.
            "name_var": str(manifest.get("name_var") or ""),
            # Longest name this module's own validation accepts. Composing a
            # longer one fails at PLAN time, after the request has been approved
            # — see _resource_name. 0 means the module states no limit.
            "name_max_length": int(manifest.get("name_max_length") or 0),
            # How long a single Terraform command may run. A Kubernetes cluster
            # takes far longer to build than a VM, and a timeout that fires part
            # way through an apply strands real, billing resources outside state.
            "command_timeout_seconds": manifest.get("command_timeout_seconds"),
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


def for_technology(code: str) -> dict | None:
    """The manifest that actually BUILDS a technology, if a dedicated one does.

    None means no blueprint claims it, and it falls to the generic path.

    This is the difference between what a recipe table says and what the platform
    does. `configure.py` carries a Debian recipe for apache, but apache is built
    by oci/apache-httpd, which renders its own Red Hat-only cloud-init and never
    calls configure.py — so asking configure.py whether Apache runs on Ubuntu
    gives an answer that is true of a code path nothing executes.
    """
    code = (code or "").strip()
    if not code:
        return None
    for bp in discover():
        if code in (bp.get("builds") or []):
            return bp
    return None


def name_limit_for_kind(kind: str) -> int:
    """Longest resource name the blueprint for this kind accepts, or 0 if it
    states none. Read from the manifest so the portal cannot disagree with the
    module's own validation rule."""
    manifest = for_resource_kind(kind)
    return int((manifest or {}).get("name_max_length") or 0)


def os_families_for_technology(code: str) -> set[str] | None:
    """OS families the thing that BUILDS this technology can configure.

    None means "no dedicated blueprint claims it" — the caller should fall back
    to the generic recipe table. An empty set means a blueprint claims it but
    declares no families, which the guard test refuses to allow.
    """
    manifest = for_technology(code)
    if manifest is None:
        return None
    return {str(f).strip().lower() for f in (manifest.get("os_families") or [])}
