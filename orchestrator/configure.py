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
#
# `module_name` is the OS module this technology comes from. Combined with the
# version the requester chose on the component detail form it becomes the stream
# to enable — `module_name: "nginx"` + version "1.24" -> `nginx:1.24`. Without a
# chosen version the fixed `module` below applies, so nothing changes for a
# request that names no version.
#
# PACKAGE AND SERVICE NAMES ARE PER OS FAMILY. Apache is `httpd` on Oracle Linux
# and `apache2` on Ubuntu, and its systemd unit is named the same as its package
# on each. A single list was fine while only Oracle Linux images were offered;
# once Ubuntu images appear on the form, a single list means the machine is told
# to install a package that does not exist and reports success anyway.
#
# `ports` sit OUTSIDE the family blocks: a port is a property of the software,
# not of the distribution.
#
# A family a technology has no block for is NOT installable there, and render()
# refuses rather than guessing — see _unsupported.
TEMPLATES: dict[str, dict] = {
    # OL9 offers nginx 1.20 (default), 1.22 and 1.24 as module streams, so the
    # version on the form is a real choice rather than a label. Ubuntu has no
    # equivalent mechanism: you get what the release carries.
    "nginx": {
        "ports": [80],
        "rhel": {"packages": ["nginx"], "services": ["nginx"], "module_name": "nginx"},
        "debian": {"packages": ["nginx"], "services": ["nginx"]},
    },
    # OL9 ships a single httpd with no streams — one honest version.
    "apache": {
        "ports": [80],
        "rhel": {"packages": ["httpd"], "services": ["httpd"]},
        "debian": {"packages": ["apache2"], "services": ["apache2"]},
    },
    # No port: Redis ships with no authentication, and opening 6379 to the subnet
    # would publish an unauthenticated data store to every host that can route to
    # it. Usable as a local cache today; exposing it needs a password, which is a
    # deliberate follow-up rather than a default.
    #
    # `module` selects an OS module stream before installing. Without it,
    # `dnf install redis` on Oracle Linux 9 gives 6.2 — the modular packages are
    # filtered out until the stream is enabled — so a catalogue entry called
    # "Redis 7" delivered Redis 6. Verified on a real VM: with the stream
    # enabled it installs 7.2.14.
    "redis7": {
        "ports": [],
        "rhel": {"packages": ["redis"], "services": ["redis"],
                 "module": "redis:7", "module_name": "redis"},
        # Debian names both the package and the unit redis-server.
        "debian": {"packages": ["redis-server"], "services": ["redis-server"]},
    },
    "java21": {
        "ports": [],
        "rhel": {"packages": ["java-21-openjdk-headless"], "services": []},
        "debian": {"packages": ["openjdk-21-jre-headless"], "services": []},
    },
    "python312": {
        "ports": [],
        "rhel": {"packages": ["python3", "python3-pip"], "services": []},
        "debian": {"packages": ["python3", "python3-pip"], "services": []},
    },
    # Same failure as redis7, found while wiring version selection up: OL9's
    # DEFAULT nodejs stream is 18, so `dnf install nodejs` under a catalogue entry
    # named "Node.js 20" delivered 18. Pinning the stream makes the name true.
    # Not boot-tested — it stays out of VERIFIED_CODES until it is.
    "nodejs20": {
        "ports": [],
        "rhel": {"packages": ["nodejs", "npm"], "services": [],
                 "module": "nodejs:20", "module_name": "nodejs"},
        "debian": {"packages": ["nodejs", "npm"], "services": []},
    },
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
#
# EVERY ENTRY ABOVE WAS PROVEN ON ORACLE LINUX. The Debian recipes added when
# Ubuntu images were offered have NOT been booted: the package and unit names
# come from Ubuntu's documented catalogue, not from a machine that ran them. A
# name that is merely plausible is exactly what delivered Redis 6.2 under a
# catalogue entry called "Redis 7", so nothing here claims Ubuntu is verified
# until a VM has been booted and asked. See VERIFIED_FAMILIES.
VERIFIED_CODES: set[str] = {"nginx", "redis7"}

# Which OS families the codes above have actually been booted on. Adding to this
# set is a claim that someone ran a machine and checked the service answered.
VERIFIED_FAMILIES: set[str] = {"rhel"}

# Enabling a module stream is a Red Hat family concept. The other families have
# no equivalent, so a profile declaring a module simply has nothing emitted.
_MODULE_ENABLE = {"rhel": "dnf module enable -y", "debian": "true", "suse": "true"}

_INSTALL = {
    "rhel": "dnf install -y",
    "debian": "apt-get update && apt-get install -y",
    "suse": "zypper install -y",
}

FAMILIES = tuple(_INSTALL)

# Opening a port is family-specific too, and getting it wrong is the quiet kind
# of failure: `firewall-cmd` does not exist on Ubuntu, so the command fails, the
# `|| echo` swallows it, and a correctly installed service sits behind a closed
# port looking exactly like a broken install.
#
# `{port}` is substituted per declared port; `reload` runs once afterwards.
_FIREWALL = {
    "rhel": {"add": "firewall-cmd --permanent --add-port={port}/tcp",
             "reload": "firewall-cmd --reload"},
    # ufw ships inactive on Ubuntu cloud images. `ufw allow` on an inactive
    # firewall still records the rule, so this is correct whether or not the
    # image has it enabled, and it never switches the firewall ON — doing that
    # unasked could cut off SSH.
    "debian": {"add": "ufw allow {port}/tcp", "reload": "ufw --force reload"},
    "suse": {"add": "firewall-cmd --permanent --add-port={port}/tcp",
             "reload": "firewall-cmd --reload"},
}

# What the portal calls an OS family, keyed by the `operating_system` string OCI
# reports for an image. Substring match, lowercased, first hit wins.
_OS_FAMILY_BY_NAME = (
    ("oracle linux", "rhel"),
    # Oracle Autonomous Linux is Oracle Linux underneath, but its name does not
    # CONTAIN "Oracle Linux" — it is "Oracle Autonomous Linux". Present in the
    # customer's tenancy (12 images), and it would have fallen through to "" and
    # been treated as unconfigurable.
    ("autonomous linux", "rhel"),
    ("red hat", "rhel"),
    ("centos", "rhel"),
    ("almalinux", "rhel"),
    ("rocky", "rhel"),
    ("ubuntu", "debian"),
    ("debian", "debian"),
    ("suse", "suse"),
    ("sles", "suse"),
)


def family_for_os(os_name: str) -> str:
    """The OS family for an image's operating system, or "" if unrecognised.

    "" is deliberately not a default of rhel: guessing rhel for an unknown OS is
    how a machine gets told to run `dnf` on something that has never heard of it.
    """
    lowered = (os_name or "").strip().lower()
    for needle, family in _OS_FAMILY_BY_NAME:
        if needle in lowered:
            return family
    return ""


def boot_report_url(reference: str, resource_kind: str) -> str:
    """Where this machine PUTs its own evidence, or "" if not configured.

    A pre-authenticated request granting object WRITES only, on a bucket that
    holds nothing else. The machine can add its report and can neither read
    another nor list the bucket, so the URL travelling in user-data — where
    anyone able to read that VM's instance metadata can see it — costs at most
    some junk objects.

    Configured rather than assumed: with no PAR set, nothing is emitted and a
    machine behaves exactly as it did before self-reporting existed.
    """
    par = (os.getenv("OCI_BOOT_REPORT_PAR_URL", "") or "").strip()
    reference = (reference or "").strip()
    if not par or not reference:
        return ""
    if not par.endswith("/"):
        par += "/"
    kind = (resource_kind or "resource").strip() or "resource"
    return f"{par}{reference}-{kind}.txt"


def _report_script(codes: list[str], packages: list[str], services: list[str],
                   ports: list[int], family: str, report_url: str) -> list[str]:
    """The self-check a machine runs on itself, as cloud-config write_files lines.

    WHY A MACHINE REPORTS ON ITSELF.

    The portal has been inferring success from Terraform exiting zero, and that
    inference has been wrong four times: a service-vm that installed nothing, an
    Apache whose runcmd was discarded, a Redis 6 sold as 7, an nginx that lost
    port 80 to httpd. Every one booted healthy and was reported provisioned.

    Asking the machine afterwards catches all of them — but Run Command, the way
    we asked, does not exist on OCI's Ubuntu images (measured: 16 agent plugins
    on Oracle Linux, 11 on Ubuntu, and it is in neither list on the latter). A
    private VM cannot be reached inbound either. So the machine reports OUTWARD,
    which works on any operating system and needs nothing installed.

    Written as a script file rather than inline commands: it is long, it contains
    quotes and loops, and every character of it would otherwise have to survive
    YAML quoting — the failure that has now cost this project two machines.
    """
    checks = [
        "#!/bin/sh",
        "# Written by the provisioning portal. Reports what this machine ACTUALLY",
        "# has, so success is evidence rather than an inference from Terraform.",
        "REPORT=/var/log/infra-portal-report.txt",
        "{",
        f"  echo 'os_family={family}'",
        f"  echo 'technologies={' '.join(codes)}'",
        "  . /etc/os-release 2>/dev/null && echo \"os=$PRETTY_NAME\"",
        "  echo '--- packages ---'",
    ]
    for package in packages:
        # Both package managers are tried because the report must be readable
        # even when the machine is not the family we think it is — which is
        # precisely the failure worth catching.
        checks.append(
            f"  (rpm -q {package} 2>/dev/null || dpkg-query -W -f='{package} ${{Version}}\\n' "
            f"{package} 2>/dev/null || echo '{package} NOT INSTALLED')")
    checks.append("  echo '--- services ---'")
    for service in services:
        checks.append(f"  echo \"{service}=$(systemctl is-active {service} 2>&1)\"")
    checks.append("  echo '--- ports ---'")
    for port in ports:
        checks.append(
            f"  echo \"http_{port}=$(curl -s -o /dev/null -m 5 -w '%{{http_code}}' "
            f"http://localhost:{port}/ 2>&1)\"")
        checks.append(f"  ss -lnt 2>/dev/null | grep ':{port} ' || echo 'nothing listening on {port}'")
    checks += [
        "  echo '--- first-boot log ---'",
        "  cat /var/log/infra-portal.log 2>/dev/null || echo '(no portal log written)'",
        "} > $REPORT 2>&1",
        # `|| true` so a failed upload never fails the boot: the report is
        # diagnostic, and a machine that works but could not phone home is a
        # better outcome than one marked broken because a bucket was unreachable.
        f"curl -s -X PUT --data-binary @$REPORT '{report_url}' >/dev/null 2>&1 || true",
    ]
    return checks


def os_family() -> str:
    """The DEFAULT family, when a request names no image.

    Only a fallback now. When the requester picked an OS image, the family comes
    from that image (see render), because a global setting cannot be right for
    two machines in one request running different operating systems.
    """
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


def profile_for(code: str, family: str = "") -> dict | None:
    """The configuration profile for a technology on one OS family.

    None means "this technology cannot be installed on this family", and callers
    must treat that as a refusal rather than a reason to fall back — falling back
    to rhel package names on an Ubuntu machine is precisely the failure this
    signature exists to prevent.

    A CONFIG_PACKAGE_MAP override may be given either per family
    ({"nginx": {"debian": {...}}}) or flat ({"nginx": {"packages": [...]}}); a
    flat override applies to whichever family is being rendered, which is how the
    setting behaved before families existed.
    """
    family = (family or os_family()).strip().lower()
    if family not in _INSTALL:
        return None
    merged = {**TEMPLATES, **_package_overrides()}
    prof = merged.get((code or "").strip())
    if not isinstance(prof, dict):
        return None

    # Per-family block, or the dict itself when an override was written flat.
    block = prof.get(family)
    if not isinstance(block, dict):
        block = prof if "packages" in prof else None
    if not isinstance(block, dict) or not block.get("packages"):
        return None

    return {"family": family,
            "packages": list(block.get("packages") or []),
            "services": list(block.get("services") or []),
            # Ports belong to the software, not the distribution, so they sit at
            # the top level — but honour a flat override that carries them.
            "ports": [int(p) for p in (prof.get("ports")
                                       or block.get("ports") or [])],
            # Optional OS module stream to enable before installing, e.g.
            # "redis:7". Without it the default stream wins, which can be a
            # major version behind what the catalogue promises. A Red Hat family
            # concept; Debian blocks simply carry none.
            "module": (block.get("module") or "").strip(),
            # The module this technology comes from, so a chosen version can be
            # turned into a stream (see module_stream).
            "module_name": (block.get("module_name") or "").strip()}


def supported_families(code: str) -> set[str]:
    """Which OS families this technology can actually be installed on.

    Drives the form: choosing an Ubuntu image must not leave software on the
    request that only has a Red Hat recipe.
    """
    return {f for f in FAMILIES if profile_for(code, f)}


def module_stream(code: str, version: str = "", family: str = "") -> str:
    """The OS module stream to enable for a technology at a chosen version.

    A version the machine ignores is exactly the bug that shipped Redis 6.2 under
    a catalogue entry called "Redis 7", so the component detail form's version is
    resolved to a real stream here or it does not reach the machine at all.
    Falls back to the profile's fixed stream when no version is chosen, which is
    what every request raised before that form does.
    """
    prof = profile_for(code, family)
    if not prof:
        return ""
    version = (version or "").strip()
    if version and prof["module_name"]:
        return f"{prof['module_name']}:{version}"
    return prof["module"]


def ports_for(components: list[dict], family: str = "") -> list[int]:
    """Every TCP port the request's technologies listen on, de-duplicated.

    The blueprint opens exactly these in the network rules and `render()` opens
    exactly these in the OS firewall, so a service can never be running behind a
    closed port (or reachable on one nobody declared).

    Scoped to the machine's OS family for the same reason: software with no
    recipe for this family installs nothing, so opening a port for it would
    publish a hole to a service that is not there.
    """
    ports: list[int] = []
    for c in components or []:
        prof = profile_for(c.get("technology_code") or "", family)
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


def render(components: list[dict], family: str = "", report_url: str = "") -> str:
    """Cloud-init user-data configuring the request's technologies, or "" when
    there is nothing to configure (or the feature is off).

    `family` is the OS family of the image THIS machine boots, resolved by the
    caller from the requester's chosen image. It defaults to CONFIG_OS_FAMILY,
    which is what every request that names no image gets — unchanged behaviour.

    A technology with no recipe for this family is SKIPPED, not guessed at.
    Rendering `dnf install httpd` onto an Ubuntu machine produces a boot that
    completes, writes a success marker, and installs nothing: the exact silent
    success this module was rewritten to stop.

    Returns plain text; the Terraform module base64-encodes it.
    """
    if not enabled():
        return ""
    family = (family or os_family()).strip().lower()
    if family not in _INSTALL:
        family = os_family()
    # (code, chosen version) — the version comes from the component detail form
    # and is "" for anything raised before it existed.
    chosen = [(c.get("technology_code"), (c.get("version") or "").strip())
              for c in (components or []) if c.get("technology_code")]
    profiles = [(c, v, profile_for(c, family)) for c, v in chosen]
    # Technologies with no recipe for this family. They are named in the marker
    # file below rather than dropped in silence, so a machine missing software
    # says why on itself.
    unsupported = sorted({c for c, _v, p in profiles if not p})
    profiles = [(c, v, p) for c, v, p in profiles if p]
    if not profiles:
        return ""

    packages: list[str] = []
    services: list[str] = []
    ports: list[int] = []
    modules: list[str] = []
    for code, version, prof in profiles:
        stream = module_stream(code, version, family)
        if stream and stream not in modules:
            modules.append(stream)
        for pkg in prof["packages"]:
            if pkg not in packages:
                packages.append(pkg)
        for svc in prof["services"]:
            if svc not in services:
                services.append(svc)
        for port in prof["ports"]:
            if port not in ports:
                ports.append(port)

    install = _INSTALL[family]
    firewall = _FIREWALL[family]
    # Records the version alongside the technology, so the marker file on the
    # machine says which version was ASKED for — the fastest way to tell a
    # mis-installed version from a mis-requested one.
    configured = ", ".join(f"{code} {version}".strip() for code, version, _ in profiles)

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
        f"      os_family={family}",
        f"      packages={' '.join(packages)}",
        "      note=Written before install; presence alone does not prove success.",
    ]
    if unsupported:
        # Named on the machine itself, so "why is X missing?" is answerable
        # there rather than only from the portal.
        lines.append(f"      not_installable_on_{family}={' '.join(unsupported)}")
    # The machine's own self-check, written as a FILE so its quotes and loops
    # never have to survive YAML quoting — the failure that has already cost this
    # project two machines.
    if report_url:
        lines.append("  - path: /usr/local/bin/infra-portal-report.sh")
        lines.append("    permissions: '0755'")
        lines.append("    content: |")
        for check in _report_script(
                [code for code, _version, _profile in profiles],
                packages, services, ports, family, report_url):
            lines.append(f"      {check}")
    lines.append("runcmd:")
    # Module streams are enabled BEFORE the install, or the default stream is
    # already resolved and the wrong major version comes down.
    for mod in modules:
        lines.append(cmd(f"{_MODULE_ENABLE[family]} {mod} || echo 'PORTAL: could not "
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
        add = firewall["add"].format(port=port)
        lines.append(cmd(f"{add} || echo 'PORTAL: could not open {port}/tcp' >> /var/log/infra-portal.log"))
    if ports:
        lines.append(cmd(f"{firewall['reload']} || true"))
    # Say on the machine what was skipped and why, so a missing service is
    # diagnosable from the VM without going back to the portal.
    for code in unsupported:
        lines.append(cmd(f"echo 'PORTAL: {code} has no install recipe for {family}; "
                         f"nothing was installed for it' >> /var/log/infra-portal.log"))
    lines.append(cmd("echo 'PORTAL: first-boot configuration finished' >> /var/log/infra-portal.log"))
    # LAST, so the report describes the finished machine rather than one still
    # installing. Its failure is swallowed: a machine that works but could not
    # upload its report is a better outcome than one that fails boot over an
    # unreachable bucket.
    if report_url:
        lines.append(cmd("/usr/local/bin/infra-portal-report.sh || true"))
    return "\n".join(lines) + "\n"
