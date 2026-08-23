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

# Where an agent-written blueprint lands (C5a). A MOUNTED directory, not part of
# the image: modules baked in at build time cannot be added to at run time, so
# without this the agent could write a module the orchestrator would never see
# and that would vanish on the next restart.
#
# Kept separate from the shipped directory on purpose. A generated blueprint is
# not a reviewed one, and the two must never become indistinguishable — every
# entry carries its origin, and a generated manifest may not take the name of a
# shipped one (see _collides).
GENERATED_DIR = Path(os.getenv("GENERATED_BLUEPRINT_DIR", "/generated/blueprints"))
GENERATED_MODULE_ROOT = Path(os.getenv("GENERATED_MODULE_DIR", "/generated/terraform"))

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


def _streams_for(builds: list) -> dict:
    """{technology: {family: [streams]}} for the codes this blueprint builds.

    Only what has been MEASURED on a machine of that family. A technology or
    family with nothing recorded is absent, and the caller must then leave the
    catalogue's list alone rather than filtering against an empty answer.
    """
    try:
        from orchestrator.configure import FAMILIES, streams_for
    except Exception:  # noqa: BLE001 - discovery must never fail closed
        return {}
    out: dict[str, dict[str, list]] = {}
    for code in {str(b) for b in builds}:
        for family in FAMILIES:
            streams = streams_for(code, family)
            if streams:
                out.setdefault(code, {})[family] = streams
    return out


def _network_tiers(resource_kind: str) -> list | None:
    """Environment tiers this resource kind has a network mapped for.

    None means the kind takes no network map at all — the shared compute subnet
    serves it, and no tier restricts it. An empty LIST is different: it consumes
    a map and nothing is mapped, so nothing can be built.
    """
    try:
        from orchestrator.oke_networks import compute_tiers, mapped_tiers
    except Exception:  # noqa: BLE001 - discovery must never fail closed
        return None
    if resource_kind == "oci-oke":
        return mapped_tiers()
    # Ordinary machines. [] here means the map is not in force and the single
    # configured subnet still serves every tier — so publish None, meaning
    # "unrestricted", rather than [] which the form reads as "nothing can be
    # built at all".
    tiers = compute_tiers()
    return tiers or None


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


def _manifest_paths(root: Path) -> list[Path]:
    try:
        return sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml"))
    except OSError:
        return []


def discover(directory: Path | None = None,
             generated: Path | None = None) -> list[dict]:
    """Every valid manifest, shipped and generated, each carrying its origin.

    Never raises: discovery failing closed would make the portal believe the
    orchestrator ships nothing, which reads as 'no automation available'.

    SHIPPED WINS. A generated manifest that claims the name or resource kind of a
    reviewed one is rejected and reported, never merged and never silently
    preferred. Otherwise an agent could shadow a blueprint that people have
    already trusted — the one failure in this area nobody would catch by reading
    a status page.
    """
    import yaml

    root = directory or BLUEPRINT_DIR
    gen_root = generated if generated is not None else GENERATED_DIR

    # Agent-written technology profiles, grouped by the blueprint they extend
    # (C6). MOST NEW COMPONENTS NEED NO NEW TERRAFORM: keycloak, kafka, mongodb
    # and their like are software on a machine, and oci/service-vm already builds
    # machines properly — resolving the image from the cloud at run time, keeping
    # it on a private subnet, making it report what it became. What they need is
    # a package, a unit and a port, which is a profile.
    #
    # So a profile EXTENDS a shipped blueprint's `builds` rather than a second
    # module being written for a resource kind that already has one — the
    # collision this registry refuses two screens down.
    extra_builds: dict[str, list[str]] = {}
    # Technologies whose INSTALL needs the public internet, beyond whatever the
    # OS family needs. An archive install fetches its software from a release
    # host (github.com for keycloak), so a machine on a subnet with only a
    # service gateway installs Oracle's packages perfectly and then fails to
    # fetch the archive. The portal must be able to refuse that up front instead
    # of spending a sandbox VM to discover it.
    extra_internet: dict[str, list[str]] = {}
    # WHICH OS FAMILIES EACH TECHNOLOGY ACTUALLY HAS A RECIPE FOR.
    #
    # `os_families` below says what the BLUEPRINT's cloud-init can handle, and
    # oci/service-vm can handle both — it builds machines, and a machine is a
    # machine. That is not the same question as whether the software on it can
    # be installed, and the portal was answering the second with the first: a
    # requester could pick an Ubuntu image for HashiCorp Vault, whose profile has
    # only an `rhel` block, and validation would accept it. The machine then
    # boots, installs nothing, and reports
    #
    #     PORTAL FAILURE: vault has no install recipe for debian
    #
    # after the request was approved and a real VM was paid for. The proof cannot
    # catch it either, because a proof carries no image and is earned on rhel.
    #
    # The recipe knows the answer — `supported_families` reads the family blocks
    # off the profile — but it lives here and the portal cannot import it. So it
    # travels in the manifest, like `needs_internet` and `refuted` before it.
    installs_on: dict[str, list[str]] = {}
    try:
        from orchestrator import configure

        for code, profile in configure._generated_profiles().items():
            on = str(profile.get("builds_on") or "").strip()
            if on:
                extra_builds.setdefault(on, []).append(code)
                # A REPOSITORY NEEDS THE INTERNET AS MUCH AS AN ARCHIVE DOES.
                # rpm.releases.hashicorp.com is no more reachable from a subnet
                # with only a service gateway than github.com is, and listing
                # only archives here left the portal able to accept a request it
                # already knew would fail.
                if (isinstance(profile.get("archive"), dict)
                        or isinstance(profile.get("repo"), dict)):
                    extra_internet.setdefault(on, []).append(code)

        for code in configure.configurable_codes():
            families = sorted(configure.supported_families(code))
            if families:
                # Empty is NOT recorded: it means this code has no profile at
                # all, and claiming "installs on nothing" for it would hide every
                # image from a technology the blueprint may configure some other
                # way. Absent means "no claim", which the portal reads as before.
                installs_on[code] = families
    except Exception:  # noqa: BLE001 — a broken profile must not hide the catalogue
        extra_builds, extra_internet, installs_on = {}, {}, {}

    found: list[dict] = []
    shipped_names: set[str] = set()
    shipped_kinds: set[str] = set()

    paths = [(p, "shipped") for p in _manifest_paths(root)]
    paths += [(p, "generated") for p in _manifest_paths(gen_root)]
    if not paths:
        return []

    for path, origin in paths:
        try:
            manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — a broken file must not hide the rest
            found.append({"ref": path.stem, "error": "manifest could not be parsed",
                          "origin": origin,
                          "builds": [], "target": "", "resource_kind": ""})
            continue
        if not isinstance(manifest, dict):
            continue
        if any(not manifest.get(f) for f in _REQUIRED_FIELDS):
            found.append({"ref": manifest.get("ref") or path.stem,
                          "error": f"manifest is missing one of {', '.join(_REQUIRED_FIELDS)}",
                          "origin": origin,
                          "builds": [], "target": manifest.get("target", ""),
                          "resource_kind": manifest.get("resource_kind", "")})
            continue

        ref = str(manifest["ref"])
        kind = str(manifest["resource_kind"])
        if origin == "shipped":
            shipped_names.add(ref)
            shipped_kinds.add(kind)
        elif ref in shipped_names or kind in shipped_kinds:
            # A generated blueprint may not take the name of a reviewed one.
            # Reported rather than dropped: silently ignoring it would leave the
            # agent believing it had published something.
            found.append({
                "ref": ref, "origin": origin, "builds": [],
                "target": str(manifest.get("target", "")), "resource_kind": kind,
                "error": ("a shipped blueprint already uses this ref or resource "
                          "kind; a generated one may not shadow it")})
            continue

        ready, missing = _readiness(manifest)
        found.append({
            "origin": origin,
            "ref": str(manifest["ref"]),
            "target": str(manifest["target"]).strip().lower(),
            "resource_kind": str(manifest["resource_kind"]),
            "module": str(manifest.get("module") or "."),
            "version": str(manifest.get("version") or ""),
            "builds": ([str(b) for b in manifest.get("builds") or []]
                       + sorted(extra_builds.get(ref, []) if origin == "shipped" else [])),
            # Which of `builds` came from an agent-written profile rather than
            # from the manifest. Reported so a reader can always tell what a
            # person put on this blueprint from what a machine added to it.
            "builds_generated": sorted(
                extra_builds.get(ref, []) if origin == "shipped" else []),
            # Which of `builds` cannot be installed without reaching the public
            # internet — the manifest's own declaration, plus any agent-written
            # profile that fetches an archive. Read by the portal so a subnet
            # with no NAT refuses the request rather than building a machine that
            # cannot finish.
            "needs_internet": sorted(set(
                [str(b) for b in manifest.get("needs_internet") or []]
                + (extra_internet.get(ref, []) if origin == "shipped" else []))),
            # {technology: families its RECIPE covers}. Narrower than
            # `os_families` below, which is about this blueprint's cloud-init.
            # A code absent here makes no claim and the portal falls back to
            # `os_families`, exactly as it did before this existed.
            "installs_on": {code: installs_on[code]
                            for code in ([str(b) for b in manifest.get("builds") or []]
                                         + (extra_builds.get(ref, [])
                                            if origin == "shipped" else []))
                            if code in installs_on},
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
            # Technologies this blueprint builds but nothing can inspect —
            # distinct from a blueprint that boots no machine at all.
            "cannot_verify": dict(manifest.get("cannot_verify") or {}),
            # Tiers this blueprint has a network for. Only blueprints that
            # consume a per-tier network map carry it; an empty list means the
            # portal has none mapped and the form must say so BEFORE approval,
            # not discover it at plan time.
            "network_tiers": _network_tiers(manifest.get("resource_kind", "")),
            # Versions each OS family can actually pin, measured on a real
            # machine. The catalogue's own list went stale — it offered nginx
            # 1.20 on an Oracle Linux 9.8 that has no such stream.
            "streams": _streams_for(manifest.get("builds") or []),
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
