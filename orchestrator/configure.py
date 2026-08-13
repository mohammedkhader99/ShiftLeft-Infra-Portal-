"""First-boot configuration for VM-based technologies (GAP-ANALYSIS.md step 4).

Terraform creates a bare virtual machine. A bare machine is not a service — which
is why most of the catalogue still needed a human after provisioning. This module
turns a request's chosen technologies into **cloud-init user-data**, so the VM
installs and enables them itself at first boot.

Why cloud-init rather than Ansible over SSH
-------------------------------------------
The VMs are private-only (no public IP) and the orchestrator runs outside the
VCN, so it cannot reach them to push configuration. Cloud-init inverts that: the
machine configures itself on boot, needing no inbound connectivity. For richer
configuration later, cloud-init can bootstrap `ansible-pull` — the VM fetching
and applying a playbook locally — which is the same pattern.

PREREQUISITE, and it is a real one: installing packages needs egress from the
private subnet (a NAT or service gateway) or an internal mirror. Without it the
boot-time install fails and you get a bare VM anyway. `render()` therefore emits
a marker file recording what was attempted, so a failed boot is diagnosable.

Package names are image-dependent — the OS image is the customer's. The defaults
target the RHEL/Oracle Linux family; override with CONFIG_PACKAGE_MAP (JSON) or
switch families with CONFIG_OS_FAMILY, without changing code.
"""

from __future__ import annotations

import json
import os

# technology code -> {packages, services, ports}. Deliberately a small, defensible
# set of services installable from the base repositories; adding one is a data
# change. `services` are enabled + started after install. `ports` are opened in
# the OS firewall AND drive the network rules, from this one declaration, so the
# two cannot disagree.
#
# A technology with no `ports` is installed and running but reachable only from
# the machine itself. That is a deliberate choice, not an omission — see redis7.
TEMPLATES: dict[str, dict] = {
    "nginx": {"packages": ["nginx"], "services": ["nginx"], "ports": [80]},
    "apache": {"packages": ["httpd"], "services": ["httpd"], "ports": [80]},
    # No port: Redis ships with no authentication, and opening 6379 to the subnet
    # would publish an unauthenticated data store to every host that can route to
    # it. Usable as a local cache today; exposing it needs a password, which is a
    # deliberate follow-up rather than a default.
    # `module` selects an OS module stream before installing. Without it,
    # `dnf install redis` on Oracle Linux 9 gives 6.2 — the modular packages are
    # filtered out until the stream is enabled — so a catalogue entry called
    # "Redis 7" delivered Redis 6. Verified on a real VM: with the stream
    # enabled it installs 7.2.14.
    "redis7": {"packages": ["redis"], "services": ["redis"], "ports": [],
               "module": "redis:7"},
    "java21": {"packages": ["java-21-openjdk-headless"], "services": [], "ports": []},
    "python312": {"packages": ["python3", "python3-pip"], "services": [], "ports": []},
    "nodejs20": {"packages": ["nodejs", "npm"], "services": [], "ports": []},
}

# Codes whose first-boot configuration has been PROVEN on a real VM: booted, and
# the service asked whether it was working. Adding a code here is a deliberate,
# reviewable claim, and it must be backed by evidence recorded below.
#
#   nginx   11 Aug 2026 — installed nginx-1.20.1, service active, listening on
#           0.0.0.0:80, curl localhost returned 200.
#   redis7  11 Aug 2026 — installed redis-7.2.14 via the redis:7 module stream,
#           service active, redis-cli PONG, set/get round-tripped, and bound to
#           127.0.0.1 only as the profile intends.
#
# Both were verified from the config this file GENERATES, not from hand-written
# cloud-init, and both VMs were destroyed afterwards.
#
# `apache` is deliberately NOT here. The certified Apache blueprint renders its
# own cloud-init from a Terraform template and never calls this module, so the
# entry below is untested — proving REQ-2026-0100 proved that template, not this
# one.
VERIFIED_CODES: set[str] = {"nginx", "redis7"}

# Enabling a module stream is a Red Hat family concept. The other families have
# no equivalent, so a profile declaring a module simply has nothing emitted.
_MODULE_ENABLE = {"rhel": "dnf module enable -y", "debian": "true", "suse": "true"}

_INSTALL = {
    "rhel": "dnf install -y",
    "debian": "apt-get update && apt-get install -y",
    "suse": "zypper install -y",
}


def os_family() -> str:
    fam = (os.getenv("CONFIG_OS_FAMILY", "rhel") or "rhel").strip().lower()
    return fam if fam in _INSTALL else "rhel"


def _package_overrides() -> dict:
    """CONFIG_PACKAGE_MAP: JSON of {code: {"packages": [...], "services": [...]}}
    merged over TEMPLATES, so package names can be corrected for a specific image
    without a code change. Malformed JSON is ignored rather than breaking a build."""
    raw = (os.getenv("CONFIG_PACKAGE_MAP") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def profile_for(code: str) -> dict | None:
    """The configuration profile for a technology, or None if it has no template."""
    merged = {**TEMPLATES, **_package_overrides()}
    prof = merged.get((code or "").strip())
    if not isinstance(prof, dict):
        return None
    return {"packages": list(prof.get("packages") or []),
            "services": list(prof.get("services") or []),
            "ports": [int(p) for p in (prof.get("ports") or [])],
            # Optional OS module stream to enable before installing, e.g.
            # "redis:7". Without it the default stream wins, which can be a
            # major version behind what the catalogue promises.
            "module": (prof.get("module") or "").strip()}


def ports_for(components: list[dict]) -> list[int]:
    """Every TCP port the request's technologies listen on, de-duplicated.

    The blueprint opens exactly these in the network rules and `render()` opens
    exactly these in the OS firewall, so a service can never be running behind a
    closed port (or reachable on one nobody declared).
    """
    ports: list[int] = []
    for c in components or []:
        prof = profile_for(c.get("technology_code") or "")
        for p in (prof or {}).get("ports", []):
            if p not in ports:
                ports.append(p)
    return ports


def configurable_codes() -> set[str]:
    """Technologies that have a first-boot configuration template."""
    return set(TEMPLATES) | {k for k in _package_overrides() if isinstance(k, str)}


def enabled() -> bool:
    """First-boot configuration is opt-in: it changes what a VM does at boot, and
    it needs subnet egress to work at all."""
    return (os.getenv("CONFIG_ENABLED", "false").strip().lower()
            in ("1", "true", "yes", "on"))


def render(components: list[dict]) -> str:
    """Cloud-init user-data configuring the request's technologies, or "" when
    there is nothing to configure (or the feature is off).

    Returns plain text; the Terraform module base64-encodes it.
    """
    if not enabled():
        return ""
    codes = [c.get("technology_code") for c in (components or []) if c.get("technology_code")]
    profiles = [(c, profile_for(c)) for c in codes]
    profiles = [(c, p) for c, p in profiles if p]
    if not profiles:
        return ""

    packages: list[str] = []
    services: list[str] = []
    ports: list[int] = []
    modules: list[str] = []
    for _code, prof in profiles:
        if prof.get("module") and prof["module"] not in modules:
            modules.append(prof["module"])
        for pkg in prof["packages"]:
            if pkg not in packages:
                packages.append(pkg)
        for svc in prof["services"]:
            if svc not in services:
                services.append(svc)
        for port in prof["ports"]:
            if port not in ports:
                ports.append(port)

    install = _INSTALL[os_family()]
    configured = ", ".join(code for code, _ in profiles)

    def cmd(text: str) -> str:
        """One runcmd entry, quoted so YAML reads it as a command.

        Emitted bare, a command containing ': ' is parsed as a MAPPING, not a
        string — `echo 'PORTAL: failed'` becomes {"echo 'PORTAL": "failed'"} and
        cloud-init aborts the entire runcmd block with "Failed to shellify".
        That is not hypothetical: it is why no service-vm ever installed anything.
        json.dumps produces a correctly escaped double-quoted scalar, which YAML
        accepts verbatim.
        """
        return f"  - {json.dumps(text)}"

    lines = [
        "#cloud-config",
        # Kept strictly ASCII: this is base64-encoded through Terraform into a
        # boot script, where a stray non-ASCII byte is a needless failure mode.
        "# Generated by the provisioning portal - first-boot configuration.",
        f"# Technologies: {configured}",
        "package_update: true",
        "write_files:",
        "  - path: /etc/infra-portal-configured",
        "    permissions: '0644'",
        "    content: |",
        f"      technologies={configured}",
        f"      packages={' '.join(packages)}",
        "      note=Written before install; presence alone does not prove success.",
        "runcmd:",
    ]
    # Module streams are enabled BEFORE the install, or the default stream is
    # already resolved and the wrong major version comes down.
    for mod in modules:
        lines.append(cmd(f"{_MODULE_ENABLE[os_family()]} {mod} || echo 'PORTAL: could not "
                         f"enable module {mod}' >> /var/log/infra-portal.log"))
    if packages:
        # `|| true` keeps a failed install from aborting the rest of cloud-init, so
        # the marker + log survive for diagnosis instead of a silent dead VM.
        lines.append(cmd(f"{install} {' '.join(packages)} || echo 'PORTAL: package install FAILED' >> /var/log/infra-portal.log"))
    for svc in services:
        lines.append(cmd(f"systemctl enable --now {svc} || echo 'PORTAL: {svc} failed to start' >> /var/log/infra-portal.log"))
    # The OS firewall, from the same declaration that drives the network rules.
    # Without this a service starts correctly and is still unreachable — which
    # looks exactly like a broken install.
    for port in ports:
        lines.append(cmd(f"firewall-cmd --permanent --add-port={port}/tcp || echo 'PORTAL: could not open {port}/tcp' >> /var/log/infra-portal.log"))
    if ports:
        lines.append(cmd("firewall-cmd --reload || true"))
    lines.append(cmd("echo 'PORTAL: first-boot configuration finished' >> /var/log/infra-portal.log"))
    return "\n".join(lines) + "\n"
