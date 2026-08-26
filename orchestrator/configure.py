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
import logging
import os
import pathlib
import shlex

from common import profile_rules

# Where the agent's own technology profiles land (C6). Separate from TEMPLATES
# for the same reason generated blueprints are separate from shipped ones: git
# history, and a reader, must be able to tell what a person reviewed from what a
# machine proposed.
#
# A profile here is NOT a claim that it works. It is a claim that it is worth
# BOOTING A MACHINE to find out — which is what the proof then does, and the
# machine reports the version it actually received.
_log = logging.getLogger(__name__)

GENERATED_PROFILE_DIR = pathlib.Path(
    os.getenv("GENERATED_PROFILE_DIR", "/generated/profiles"))

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
#
# `expects` is the version THE CATALOGUE NAME PROMISES, and `version_command` is
# how the machine is asked what it actually received. The two exist because a
# recipe and a catalogue entry can agree only by luck otherwise: "Redis 7"
# installing a package called `redis` delivered Redis 6.2 on Oracle Linux, and
# nothing noticed until someone logged in. An entry whose name names a version
# and whose recipe cannot pin one is a promise waiting to be broken — so the
# machine now reports the version it has and the portal compares.
TEMPLATES: dict[str, dict] = {
    # OL9 offers nginx 1.20 (default), 1.22 and 1.24 as module streams, so the
    # version on the form is a real choice rather than a label. Ubuntu has no
    # equivalent mechanism: you get what the release carries.
    "nginx": {
        "ports": [80],
        # No `expects`: the requester chooses the version, so the promise is
        # whatever they picked. render() supplies it per request.
        "version_command": "nginx -v 2>&1",
        # The streams Oracle Linux 9.8 ACTUALLY offers, measured on the machine
        # from REQ-2026-0146 with `dnf module list nginx` on 16 Aug 2026:
        # 1.22, 1.24, 1.26.
        #
        # The portal offered 1.20, 1.22, 1.24 — a seeded list that was true once
        # and went stale. There is no 1.20 stream on 9.8, so `dnf module enable
        # nginx:1.20` fails and the request lands verify-failed; the 1.20.1 that
        # installs comes from the non-modular base package, so the requester got
        # what they asked for by accident while the pinning step had failed.
        # 1.26 was silently unavailable.
        #
        # Declared beside the recipe rather than seeded into the catalogue,
        # because this is a property of the OS the recipe installs from, and the
        # catalogue had no way to know it had changed.
        "rhel": {"packages": ["nginx"], "services": ["nginx"], "module_name": "nginx",
                 "streams": ["1.22", "1.24", "1.26"]},
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
        "expects": "7",
        "version_command": "redis-server --version 2>&1",
        # Measured the same way: OL 9.8 offers exactly one redis stream, 7.
        "rhel": {"packages": ["redis"], "services": ["redis"],
                 "module": "redis:7", "module_name": "redis", "streams": ["7"]},
        # Debian names both the package and the unit redis-server.
        "debian": {"packages": ["redis-server"], "services": ["redis-server"]},
    },
    "java21": {
        "ports": [],
        "expects": "21",
        "version_command": "java -version 2>&1",
        "rhel": {"packages": ["java-21-openjdk-headless"], "services": []},
        "debian": {"packages": ["openjdk-21-jre-headless"], "services": []},
    },
    "python312": {
        "ports": [],
        "expects": "3.12",
        "version_command": "python3 --version 2>&1",
        # `python3` on Oracle Linux 9 is Python 3.9 — measured 3.9.25 on
        # REQ-2026-0139, under a catalogue entry called "Python 3.12". The 3.12
        # interpreter is a SEPARATE package, confirmed available on the machine
        # itself: python3.12-3.12.13 and python3.12-pip-23.2.1, both ol9_appstream.
        #
        # Installed ALONGSIDE the system python, never replacing it: `dnf` itself
        # runs on 3.9, and repointing the `python3` alternative would break
        # package management on the machine. So the interpreter is at
        # /usr/bin/python3.12 and the check asks for it by name.
        "rhel": {"packages": ["python3.12", "python3.12-pip"], "services": [],
                 "version_command": "python3.12 --version 2>&1"},
        # Ubuntu 24.04's `python3` IS 3.12 (measured 3.12.3 on REQ-2026-0140), so
        # this side is left exactly as it was proven.
        "debian": {"packages": ["python3", "python3-pip"], "services": []},
    },
    # Same failure as redis7, found while wiring version selection up: OL9's
    # DEFAULT nodejs stream is 18, so `dnf install nodejs` under a catalogue entry
    # named "Node.js 20" delivered 18. Pinning the stream makes the name true.
    # Not boot-tested — it stays out of VERIFIED_CODES until it is.
    "nodejs20": {
        "ports": [],
        "expects": "20",
        "version_command": "node --version 2>&1",
        "rhel": {"packages": ["nodejs", "npm"], "services": [],
                 "module": "nodejs:20", "module_name": "nodejs"},
        "debian": {"packages": ["nodejs", "npm"], "services": []},
    },
}

# (technology, OS family) pairs whose first-boot configuration has been PROVEN on
# a real VM: booted, and the service asked whether it was working. Each pair is a
# deliberate, reviewable claim and must be backed by the evidence recorded below.
#
# A PAIR, not two lists. This was a set of codes and a set of families, which can
# only express their cross-product — so recording that nginx works on Ubuntu
# would silently have claimed redis7 does too, on the strength of a machine
# nobody ever booted. That is the same shape as the bug that delivered Redis 6.2
# under a catalogue entry called "Redis 7": a plausible name standing in for a
# checked one.
#
#   nginx / rhel     11 Aug 2026 — installed nginx-1.20.1, service active,
#                    listening on 0.0.0.0:80, curl localhost returned 200.
#   redis7 / rhel    11 Aug 2026 — installed redis-7.2.14 via the redis:7 module
#                    stream, service active, redis-cli PONG, set/get
#                    round-tripped, bound to 127.0.0.1 only as the profile
#                    intends.
#   nginx / debian   15 Aug 2026 — REQ-2026-0138 on Ubuntu 24.04.4 LTS: installed
#                    nginx 1.24.0-2ubuntu7.15, unit active, firewall_80=open, and
#                    a DIFFERENT machine on the subnet
#                    (test-req-2026-0134-apache-01) fetched it over the network:
#                    HTTP 200, `Server: nginx/1.24.0 (Ubuntu)`, real page served.
#
#                    The third-party fetch is what makes this evidence rather
#                    than a process listing. Two earlier attempts at the same
#                    request looked healthy from inside the machine and were not:
#                    REQ-2026-0134 had no nginx on it at all (the subnet had no
#                    route to apt), and REQ-2026-0136 had nginx running and
#                    answering on loopback while every other host got "No route
#                    to host" (the firewall step used ufw, which OCI's Ubuntu
#                    images do not ship). Both were recorded `provisioned`.
#
# All verified from the config this file GENERATES, not from hand-written
# cloud-init.
#
# WHAT THIS RECORDS. Proven (technology, OS family) pairs, whatever PROVED them.
# Not "pairs proven through configure.py" — that reading kept apache out on the
# grounds that its blueprint renders its own cloud-init, and the certification
# gate then refused to certify a web server that has been serving traffic since
# REQ-2026-0134. A false refusal costs trust exactly as a missed failure does.
#
# The recipe below for apache is still untested — nothing installs from it,
# because the certified blueprint uses its own template. What is proven is the
# COMBINATION, and that is what the gate asks about.
#
# STILL UNPROVEN ON DEBIAN: redis7, java21, python312, nodejs20. Their package
# and unit names come from Ubuntu's documented catalogue, not from a machine that
# ran them.
VERIFIED: set[tuple[str, str]] = {
    ("nginx", "rhel"),
    ("redis7", "rhel"),
    ("nginx", "debian"),
    # 15 Aug 2026 — REQ-2026-0139 (Oracle Linux 9.8) and REQ-2026-0140
    # (Ubuntu 24.04.4), one machine each with all five technologies on it. Each
    # machine reported the version it actually received and compared it with what
    # the catalogue name promises:
    #
    #   java21     OL9 java-21-openjdk-headless 21.0.11 | Ubuntu 21.0.11
    #   redis7     OL9 redis 7.2.14 (redis:7 stream)    | Ubuntu redis-server 7.0.15
    #   python312  Ubuntu python3 3.12.3
    #   nodejs20   OL9 nodejs 20.20.2 (nodejs:20 stream)
    ("java21", "rhel"),
    ("java21", "debian"),
    ("redis7", "debian"),
    ("python312", "debian"),
    ("nodejs20", "rhel"),
    # 15 Aug 2026 — REQ-2026-0146, Oracle Linux 9.8, proving the FIXED recipe:
    # python3.12-3.12.13 and python3.12-pip-23.2.1 installed, and the machine
    # answered `python3.12 --version` with 3.12.13. The system python is
    # untouched at 3.9, which is what dnf needs.
    #
    # The pair it replaces was refuted at 3.9.25 on REQ-2026-0139. A recipe was
    # changed, a machine was booted, and the claim moved on evidence — which is
    # the only way anything gets into this set.
    ("python312", "rhel"),
    # 15 Aug 2026 — REQ-2026-0134, Oracle Linux 9.8, via the oci/apache-httpd
    # blueprint's own template rather than this module: httpd 2.4.62 installed,
    # unit active, port 80 open in firewalld, HTTP 200. It then served as the
    # third-party client that proved nginx was reachable on REQ-2026-0138, which
    # is about as thoroughly exercised as a machine here gets.
    #
    # rhel only. The template has a Debian branch and no machine has ever run it,
    # and the blueprint declares rhel alone for that reason.
    ("apache", "rhel"),
}

# Combinations PROVEN NOT to deliver what the catalogue name promises.
#
# Stronger than absence from VERIFIED, and kept apart from it deliberately:
# "nobody has tried this" and "we tried it and it delivered the wrong thing" call
# for opposite treatment. The first is a gap to fill; the second is a promise the
# portal must stop making until the recipe is fixed.
#
#   python312 / rhel   RETIRED 15 Aug 2026, and worth reading as a pattern.
#                      REQ-2026-0139 measured 3.9.25 because the recipe asked for
#                      `python3`. The recipe now asks for `python3.12`, so the
#                      measurement is of something that no longer exists and the
#                      refusal goes with it. The combination is UNPROVEN again —
#                      not verified — and stays out of VERIFIED until a machine
#                      says otherwise.
#
#                      A refutation is evidence about a RECIPE, not a law about an
#                      operating system. Leaving it in place after the recipe
#                      changed would also have been a trap: the form would refuse
#                      the combination, so nobody could raise the request that
#                      would prove the fix.
#   nodejs20 / debian  15 Aug 2026, REQ-2026-0140: the recipe installs `nodejs`,
#                      and Ubuntu 24.04 carries 18.19.1. Its repositories have no
#                      Node 20 at all.
#
#                      STANDS BY DECISION, not merely by measurement. The only fix
#                      is a third-party apt source (NodeSource), and the owner of
#                      this estate chose on 15 Aug 2026 not to take packages from
#                      outside the organisation's control onto its machines. Node
#                      20 is therefore Oracle-Linux-only, where the nodejs:20
#                      module stream delivers it properly (measured 20.20.2).
#
#                      So this entry is not waiting to be fixed. It records a
#                      capability the platform has deliberately declined.
#
# Exactly the shape of the bug that delivered Redis 6.2 under an entry called
# "Redis 7" — twice over, and invisible until the machine was asked its version.
REFUTED: dict[tuple[str, str], str] = {
    ("nodejs20", "debian"):
        "Ubuntu 24.04's `nodejs` is Node 18, not Node 20 (measured: 18.19.1)",
}

# Derived, and kept only because other modules already read them. Neither is the
# truth on its own: read either in isolation and nginx-on-Ubuntu looks like
# redis7-on-Ubuntu. VERIFIED is the record; these are conveniences.
VERIFIED_CODES: set[str] = {code for code, _family in VERIFIED}
VERIFIED_FAMILIES: set[str] = {family for _code, family in VERIFIED}


def refusal(code: str, family: str) -> str:
    """Why this combination must not be built, or "" if there is no such evidence.

    Only ever populated by a machine that was built and asked. A guess belongs in
    a comment, not here.
    """
    return REFUTED.get(((code or "").strip(), (family or "").strip().lower()), "")


def is_verified(code: str, family: str) -> bool:
    """Whether THIS technology has been booted on THIS OS family.

    The question worth asking. `code in VERIFIED_CODES and family in
    VERIFIED_FAMILIES` answers a different and more generous one.
    """
    return ((code or "").strip(), (family or "").strip().lower()) in VERIFIED

# Enabling a module stream is a Red Hat family concept. The other families have
# no equivalent, so a profile declaring a module simply has nothing emitted.
_MODULE_ENABLE = {"rhel": "dnf module enable -y", "debian": "true", "suse": "true"}
# How each family adds a vendor repository. Only the Red Hat family is wired:
# Debian uses signed-by keyrings and sources.list entries, which is a different
# shape and needs its own proof on a real machine before it is offered.
# `false` alone, with NO trailing comment. The rendered line is
# `<add-repo> <url> || echo 'PORTAL FAILURE: ...'`, so a `#` here commented out
# the failure marker that follows it on the same line: the step failed silently
# and the machine reported nothing wrong. A placeholder that disables the alarm
# it exists to trigger is worse than no placeholder.
# The release package that enables EPEL on an Oracle Linux 9 machine. Oracle
# ships it in its OWN repositories, so enabling EPEL needs no route to the
# internet — which matters, because most subnets here have a service gateway and
# nothing else. It is a fixed name from profile_rules.RELEASE_PACKAGES, never a
# discovered one.
_EPEL_RELEASE = "oracle-epel-release-el9"

# What the machine's package search currently does. BUMP THIS whenever it is
# taught to look somewhere new — a negative from an older generation stops
# binding automatically, and the rung runs again rather than resting on an
# answer to a question nobody asked.
#
#   1  exact candidate names, EPEL enabled, control probe        (C7)
#   2  + a wildcard search on the stem when the names fail       (C7b)
SEARCH_GENERATION = 2


def candidate_packages(code: str) -> list[str]:
    """The names this technology might actually be packaged under, best first.

    THE RULE THAT REPLACES A DICTIONARY. `dnf install rabbitmq` finds nothing;
    the package is `rabbitmq-server`. `redis7` is a catalogue name carrying a
    version, and the package is `redis`. Each of these cost a machine to learn
    and was then written down by hand, one technology at a time.

    Cheap string shapes, all checked in ONE boot because they are metadata
    queries rather than installs — so the machine answers every hypothesis for
    the price of answering one. Every name is validated: it is handed to the
    package manager as root, and a catalogue code is not a trusted string.
    """
    code = (code or "").strip().lower()
    if not profile_rules.CODE.match(code):
        return []
    shapes = [code, f"{code}-server"]
    # A trailing version in the catalogue name is a label, not part of the
    # package: redis7 -> redis, nodejs20 -> nodejs, python312 -> python3.
    bare = code.rstrip("0123456789")
    if bare and bare != code:
        shapes += [bare, f"{bare}-server"]
    out: list[str] = []
    for name in shapes:
        if name not in out and profile_rules.CODE.match(name):
            out.append(name)
    return out


_REPO_ADD = {"rhel": "dnf config-manager --add-repo",
             "debian": "false",
             "suse": "false"}

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
             "reload": "firewall-cmd --reload",
             # How the machine checks its OWN work. firewalld answers directly.
             "query": "firewall-cmd --query-port={port}/tcp"},
    # OCI's Ubuntu images DO NOT SHIP ufw. This said they did — "ufw ships
    # inactive on Ubuntu cloud images, and `ufw allow` on an inactive firewall
    # still records the rule" — and it was simply wrong. The images filter with
    # iptables rules baked into /etc/iptables/rules.v4 that REJECT everything but
    # SSH, `ufw allow` failed with command-not-found, and the reject rule stayed.
    #
    # REQ-2026-0136 is what that costs: nginx installed, running, listening on
    # :80, and unreachable from any other machine on the subnet. The portal
    # called it provisioned. The machine had even logged the failure.
    #
    # So: use ufw when it is genuinely there and active — overwriting a live ufw
    # with raw iptables rules would have them wiped on its next reload — and
    # otherwise edit iptables directly, which is what the image actually uses.
    "debian": {
        "add": ("(command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | "
                "grep -q active && ufw allow {port}/tcp) || "
                "iptables -I INPUT -p tcp --dport {port} -j ACCEPT"),
        # Rules added at runtime are lost on reboot unless written down.
        "reload": ("netfilter-persistent save 2>/dev/null || "
                   "iptables-save > /etc/iptables/rules.v4 2>/dev/null"),
        "query": "iptables-save 2>/dev/null | grep -q -- '--dport {port} '",
    },
    "suse": {"add": "firewall-cmd --permanent --add-port={port}/tcp",
             "reload": "firewall-cmd --reload",
             "query": "firewall-cmd --query-port={port}/tcp"},
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


def _report_script(wanted: list[tuple[str, str]], packages: list[str],
                   services: list[str], ports: list[int], family: str,
                   report_url: str,
                   archives: list[tuple[str, dict]] | None = None,
                   repos: list[tuple[str, dict]] | None = None,
                   candidates: dict[str, tuple[str, list[str]]] | None = None,
                   containers: list[tuple[str, dict]] | None = None
                   ) -> list[str]:
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
        f"  echo 'technologies={' '.join(code for code, _v in wanted)}'",
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
    for code, spec in (containers or []):
        key = profile_rules.report_key(code)
        # THE DIGEST THAT ACTUALLY ARRIVED, not the one we asked for. Asking
        # podman what it has closes the gap between the recipe and the machine:
        # a pull that silently resolved elsewhere, or a stale image already on
        # the host, would otherwise be indistinguishable from the pinned one.
        want = spec["image"] + "@" + spec["digest"]
        pinned = shlex.quote(want)
        # COMPARED ON THE MACHINE, not merely reported. The digest was being
        # printed and nothing read it: verdict() judges `key=failed`, http_,
        # version_ and firewall_ lines, and an `image_x=docker.io/...@sha256:...`
        # line matched none of them, so a machine running an entirely different
        # image would have passed. An honest signal the deciding code cannot
        # see is the shape of most defects in this project.
        #
        # AND THIS REPLACES THE VERSION QUESTION for a container. Asking a
        # container its version invites a wrong answer: library/rabbitmq's
        # org.opencontainers.image.version label is "24.04", which is Ubuntu's
        # version, not RabbitMQ's. The digest identifies what is running exactly,
        # which is more than a version string can do.
        checks.append(
            f"  GOTIMG=$(podman image inspect {pinned} "
            f"--format '{{{{index .RepoDigests 0}}}}' 2>/dev/null)")
        checks.append(
            f'  echo "image_{key}=$(test "$GOTIMG" = {pinned} '
            f'&& echo "match ({spec["digest"][:19]}...)" '
            f'|| echo "MISMATCH got ${{GOTIMG:-nothing}}")"')
        checks.append(
            f"  echo \"container_{key}=$(podman ps --filter name=^{code}$ "
            f"--filter status=running --format '{{{{.Names}}}}' 2>/dev/null "
            f"| head -1 | grep -q . && echo running || echo failed)\"")
        # WHAT IS LISTENING INSIDE THE CONTAINER, which is a different question
        # from what is published on the host. A published port binds on the host
        # whether or not anything inside is listening, so the host-side socket
        # diff cannot narrow anything: podman's proxy answers either way.
        #
        # `library/rabbitmq` DECLARES six ports — AMQP, AMQPS, epmd, clustering
        # and two Prometheus endpoints — and a default container listens on far
        # fewer. Opening all six in the firewall of a real machine is more
        # surface than the service needs.
        #
        # READ FROM /proc, not from `ss`: /proc/net/tcp exists in every Linux
        # container without anything being installed, and a great many images
        # carry no networking tools at all. State 0A is LISTEN; the address is
        # hex, converted by printf rather than by a gawk extension the image's
        # awk may not have.
        awk_listen = """awk '$4=="0A"{split($2,a,":"); print a[2]}'"""
        to_decimal = """while read H; do printf '%d\\n' "0x$H"; done"""
        # POLLED, NOT SAMPLED ONCE. The first container pass publishes no
        # ports by design, so the port-wait loop had nothing to wait for and
        # this ran seconds after `systemctl start` — before RabbitMQ had bound
        # anything. `listening_inside=none` then meant "we looked too early",
        # and the narrowing pass had nothing to narrow to.
        #
        # The wait was built to depend on the very thing it was meant to
        # discover. Bounded at three minutes, and it returns the moment
        # something binds, so a fast container costs nothing.
        checks.append("  LI=")
        checks.append("  for _ in $(seq 1 36); do")
        checks.append(
            f"    LI=$(for F in /proc/net/tcp /proc/net/tcp6; do "
            f"podman exec {code} cat $F 2>/dev/null; done "
            f"| {awk_listen} | sort -u | {to_decimal} "
            f"| sort -un | paste -sd, -)")
        checks.append('    [ -n "$LI" ] && break')
        checks.append("    sleep 5")
        checks.append("  done")
        checks.append(f'  echo "listening_inside_{key}=${{LI:-none}}"')

        data_dir = spec.get("data_dir")
        if data_dir:
            # A CONTAINER WRITING TO THE BOOT VOLUME LOOKS EXACTLY LIKE ONE
            # WRITING TO ITS OWN. The difference only surfaces when the volume
            # is detached and the data is not on it.
            checks.append(
                f'  echo "data_volume_{key}=$(mountpoint -q '
                f'{shlex.quote(data_dir)} && echo mounted || echo not-mounted)"')

    # --- what this machine could have installed instead (C7) -------------------
    #
    # THE MACHINE IS ALREADY HERE AND ALREADY KNOWS. REQ-2026-0188 guessed
    # `dnf install rabbitmq`, was told "not installed", and stopped — while the
    # machine reporting that had dnf and the complete repository metadata in
    # front of it and could have named `rabbitmq-server` in one command. Every
    # serious defect in this project has this shape: an honest signal the
    # deciding code cannot see. Curating a dictionary per technology is the
    # expensive way to learn what one query answers.
    #
    # ONLY FOR PACKAGES THAT ARE MISSING, so a working install stays quiet and
    # costs nothing. Metadata queries only — the machine REPORTS candidates and
    # never installs one it discovered, because a name found at run time has
    # passed no allow-list. It goes back through profile_rules and is installed,
    # if at all, on the next rung.
    if candidates:
        checks.append("  echo '--- available ---'")
    for code, (declared, names) in (candidates or {}).items():
        key = profile_rules.report_key(code)
        listed = " ".join(shlex.quote(n) for n in names)
        # The bare stem, so `dotnet8` searches for *dotnet*. Validated by the
        # same allow-list as every other name here: it reaches a command line
        # that root runs.
        stem = code.rstrip("0123456789") or code
        if not profile_rules.CODE.match(stem):
            stem = code
        # GUARDED ON THE PACKAGE THAT WAS DECLARED, not on the technology code.
        # `redis7` declares the package `redis`, so `rpm -q redis7` fails on a
        # perfectly healthy machine and discovery would run on every boot,
        # every time, for a recipe that worked — the same false-negative shape
        # as grepping repolist for `vault` when the repository is `hashicorp`.
        checks.append(
            f"  if rpm -q {shlex.quote(declared)} >/dev/null 2>&1; then :; else")
        # EPEL, pulled in only when the configured repositories answer nothing.
        # A fixed name from profile_rules.RELEASE_PACKAGES, never a discovered
        # one, and Oracle ships it in its own repositories so this needs no
        # route to the internet.
        checks.append("    FOUND=")
        checks.append(f"    for N in {listed}; do")
        checks.append(
            '      FOUND=$(dnf --quiet repoquery --qf "%{name}|%{repoid}" '
            '"$N" 2>/dev/null | head -1)')
        checks.append('      [ -n "$FOUND" ] && break')
        checks.append("    done")
        checks.append('    if [ -z "$FOUND" ]; then')
        # EPEL, AND WHETHER IT ACTUALLY ARRIVED. REQ-2026-0189 reported
        # `available_rabbitmq=none` and nobody could tell whether RabbitMQ is
        # genuinely absent or whether this step failed and only Oracle's base
        # repositories were ever searched — every command here ends in
        # `|| true`. A missing measurement that reads as a measured zero is the
        # exact defect this whole increment exists to end, committed in the
        # fixing of it.
        checks.append(
            f"      if rpm -q {_EPEL_RELEASE} >/dev/null 2>&1; then")
        checks.append(f'        echo "epel_{key}=present"')
        checks.append("      else")
        checks.append(
            f"        dnf install -y {_EPEL_RELEASE} >/dev/null 2>&1 || true")
        checks.append(
            f"        if rpm -q {_EPEL_RELEASE} >/dev/null 2>&1; then")
        checks.append(f'          echo "epel_{key}=added"')
        checks.append("        else")
        # NOT the word `failed`: verdict() treats any `key=failed` as a broken
        # machine, and EPEL being unavailable is a fact about the search, not
        # about the health of the host.
        checks.append(f'          echo "epel_{key}=unavailable"')
        checks.append("        fi")
        checks.append("      fi")
        # INSTALLED IS NOT ENABLED, and REQ-2026-0191 proved it on a machine.
        # Oracle ships `oracle-epel-release-el9` ON the OL9 image, so the check
        # above reported `present` — and the repository it carries is defined
        # with enabled=0, so it was never searched:
        #
        #   epel_rabbitmq=present
        #   searched_rabbitmq=ol9_UEKR8,ol9_addons,ol9_appstream,
        #                     ol9_baseos_latest,ol9_ksplice,ol9_oci_included
        #
        # DISCOVERED, NOT NAMED. The repository id differs between Oracle Linux
        # releases, and hard-coding `ol9_developer_EPEL` would be the same
        # recalled-rather-than-measured mistake as Vault's `expects: "1"`. Only
        # repositories ALREADY DEFINED on this machine are enabled — every one
        # of them from Oracle's own release package — so nothing new is trusted.
        checks.append(
            "      for R in $(dnf repolist --disabled 2>/dev/null "
            "| awk 'NR>1 {print $1}' | grep -i epel); do "
            'dnf config-manager --enable "$R" >/dev/null 2>&1 || true; done')
        checks.append(f"      for N in {listed}; do")
        checks.append(
            '        FOUND=$(dnf --quiet repoquery --qf "%{name}|%{repoid}" '
            '"$N" 2>/dev/null | head -1)')
        checks.append('        [ -n "$FOUND" ] && break')
        checks.append("      done")
        checks.append("    fi")
        # AND IF NAMING IT DID NOT WORK, SEARCH FOR IT.
        #
        # REQ-2026-0197 asked for .NET 8. The candidate names — dotnet8,
        # dotnet8-server, dotnet, dotnet-server — do not exist on Oracle Linux 9,
        # and the machine said so honestly. But .NET IS there: the packages carry
        # the version as a dotted suffix (dotnet-sdk-8.0), a shape the name rule
        # does not generate and never will, because every ecosystem names its
        # packages differently.
        #
        # GUESSING NAMES IS THE WRONG INSTRUMENT. dnf can search, and a search
        # finds what no amount of shape-guessing would. The machine reports what
        # it found and the portal chooses — the same division as everywhere else
        # here, because a name picked on the machine has passed no allow-list.
        #
        # Bounded at 20: a wildcard on a common stem can match a long tail of
        # -devel, -debuginfo and -doc packages, and a report nobody can read is
        # a report nobody reads.
        checks.append(f"    if [ -z \"$FOUND\" ]; then")
        checks.append(
            f'      MATCHES=$(dnf --quiet repoquery --qf "%{{name}}|%{{repoid}}" '
            f'"*{stem}*" 2>/dev/null | sort -u | head -20 | paste -sd, -)')
        checks.append(f'      echo "matches_{key}=${{MATCHES:-none}}"')
        checks.append("    fi")

        # WHICH SEARCH THIS WAS. Bumped whenever the machine is taught to look
        # somewhere new, and a negative only binds when it came from a machine
        # running the CURRENT one.
        #
        # REQ-2026-0199 is why. REQ-2026-0197 had refuted `dnf install dotnet8`
        # honestly — EPEL enabled, control probe passing, four candidate names
        # tried — so the ladder skipped the rung in ten seconds without building
        # anything, and the wildcard search deployed an hour earlier never ran.
        # The memory was right and stale, and the soundness rule could not see
        # the difference because it only asked whether the search was sound BY
        # THE STANDARD OF ITS OWN DAY.
        #
        # This is the same trick as recipe_memory._SCHEME, for the same reason:
        # when what we ASK changes, answers to the older question stop counting,
        # and nobody has to remember to add another condition next time.
        checks.append(f'    echo "search_generation_{key}={SEARCH_GENERATION}"')

        # THE CONTROL PROBE, which is what makes `none` falsifiable. `bash` is
        # in the base repositories of every image this portal builds; if the
        # query cannot find even that, the query itself is broken and `none`
        # says nothing whatsoever about the software being looked for.
        # WHICH REPOSITORIES WERE ACTUALLY LOOKED IN.
        #
        # REQ-2026-0190 reported `epel_rabbitmq=present, queryable=yes,
        # available=none` — and that still could not settle the question, because
        # `rpm -q <release package>` proves the package is INSTALLED, not that the
        # repository it carries is ENABLED. A repo can sit in
        # /etc/yum.repos.d with enabled=0 and be searched by nothing. So
        # "RabbitMQ is not in EL9 or EPEL" and "EPEL was never searched" stayed
        # indistinguishable, which is the whole failure this reporting exists to
        # end, one level deeper.
        #
        # dnf's own enabled list is the only thing that answers it. Sanitised to
        # a comma-joined single line because verdict() parses key=value per line
        # and repolist prints a table.
        checks.append(
            "    REPOS=$(dnf --quiet repolist --enabled 2>/dev/null "
            "| awk 'NR>1 {print $1}' | paste -sd, -)")
        checks.append(f'    echo "searched_{key}=${{REPOS:-unknown}}"')
        checks.append(
            '    PROBE=$(dnf --quiet repoquery --qf "%{name}" bash '
            '2>/dev/null | head -1)')
        checks.append(
            f'    echo "queryable_{key}=$(test -n "$PROBE" && echo yes '
            f'|| echo no)"')
        checks.append(f'    echo "available_{key}=${{FOUND:-none}}"')
        checks.append("  fi")
    # Archive installs have no package for rpm -q to find, so asking about one
    # would report NOT INSTALLED for software that installed perfectly. The
    # evidence for an archive is its destination existing — and "failed" is the
    # word used because the verdict parser already treats key=failed as broken.
    for code, spec in (repos or []):
        # Evidence the repository is actually there, separate from whether the
        # package installed: "the repo was not added" and "the package is not in
        # it" need different fixes, and one failure line cannot say which.
        #
        # TESTED BY THE FILE, NOT BY THE TECHNOLOGY NAME. `dnf config-manager
        # --add-repo <url>` writes /etc/yum.repos.d/<basename of the url>, and
        # that basename is the VENDOR's — hashicorp.repo, not vault.repo. An
        # earlier version grepped `dnf repolist` for the technology code and
        # would have reported repo_vault=failed on a machine where HashiCorp's
        # repository had been added perfectly, failing the proof for a reason
        # that was not true.
        key = profile_rules.report_key(code)
        release = spec.get("release_package")
        if release:
            # Asked of rpm, not of the filesystem: a release package may write
            # its definitions under any name it likes, and the question that
            # matters is whether the package that carries them is installed.
            checks.append(
                f'  echo "repo_{key}=$(rpm -q {shlex.quote(release)} '
                f'>/dev/null 2>&1 && echo present || echo failed)"')
            continue
        basename = (spec.get("url", "").rstrip("/").rsplit("/", 1)[-1]
                    or f"{code}.repo")
        checks.append(
            f'  echo "repo_{key}=$(test -f /etc/yum.repos.d/{shlex.quote(basename)} '
            f'&& echo present || echo failed)"')
    for code, spec in (archives or []):
        dest = shlex.quote(spec.get("dest", f"/opt/{code}"))
        # NON-EMPTY, not merely present: the unpack step creates the directory,
        # so `test -d` was true on every machine including ones where the fetch
        # 404'd and nothing was ever written into it.
        #
        # The key is sanitised because verdict() only parses a line whose key is
        # a Python identifier, and catalogue codes contain hyphens — so
        # `archive_oracle-db=failed` was silently read as healthy.
        key = profile_rules.report_key(code)
        checks.append(
            f'  echo "archive_{key}=$(test -n \"$(ls -A {dest} 2>/dev/null)\" '
            f'&& echo present || echo failed)"')
    # --- versions -------------------------------------------------------------
    #
    # THE half of the question the report never asked. "the package installed" and
    # "the machine has what the catalogue promised" are different facts, and
    # conflating them is how Redis 6.2 shipped under an entry called "Redis 7".
    #
    # The comparison happens ON THE MACHINE rather than in the portal, so the
    # report is self-contained evidence: a human reading it sees what was wanted,
    # what arrived, and whether they match, without holding the catalogue in their
    # head. The first number-looking token is extracted because every one of these
    # tools announces itself differently — `Python 3.12.3`, `v20.11.0`,
    # `openjdk version "21.0.5"`, `Redis server v=7.2.14 sha=...`.
    #
    # Matched as a PREFIX with a dot boundary, never a substring: `7` appears in
    # `6.2.7`, and a substring test would have passed the very bug this exists to
    # catch.
    # The family's own command wins where it declares one. Oracle Linux installs
    # 3.12 ALONGSIDE the system python, so it answers on `python3.12` while Ubuntu
    # answers on `python3` — asking the wrong one would report a working machine
    # as broken, and a false alarm costs trust as surely as a missed failure.
    # MERGED, not TEMPLATES alone. A generated profile carries its own
    # version_command and expects, and reading only the shipped table meant an
    # agent-written recipe was never version-checked at all — the machine
    # installed something, reported it active, and nobody ever asked which
    # version arrived. That silence is precisely how Redis 6.2 shipped as 7.
    # _package_overrides() included, matching profile_for(). Without it a
    # technology introduced solely by CONFIG_PACKAGE_MAP was installed and
    # never asked what it got, and an override's own `expects` was ignored
    # in favour of the shipped one — a redis7 override pinning 8 was still
    # checked against 7.
    merged = {**TEMPLATES, **_generated_profiles(), **_package_overrides()}

    def _command(code: str) -> str:
        spec = merged.get(code, {})
        return (spec.get(family, {}).get("version_command")
                or spec.get("version_command", ""))

    versioned = [(code, wanted_version or merged.get(code, {}).get("expects", ""),
                  _command(code))
                 for code, wanted_version in wanted]
    # `cmd`, NOT `w and cmd`. An empty `expects` used to drop the technology from
    # this list entirely, so the machine was never asked — and a question never
    # asked is the silence that shipped Redis 6.2 under an entry called Redis 7.
    # Software installed from a vendor's own repository has no promised version
    # (the repository serves whatever is current), which made "no promise" and
    # "no evidence" the same thing. They are not: ASK ALWAYS, compare only when
    # a promise exists, and say plainly which of the two happened.
    versioned = [(c, w, cmd) for c, w, cmd in versioned if cmd]
    if versioned:
        checks.append("  echo '--- versions ---'")
    for code, want, command in versioned:
        vkey = profile_rules.report_key(code)
        # THE BINARY FIRST. REQ-2026-0188 reported `version_rabbitmq=UNPROMISED
        # (12)` for software that was not installed at all: the command failed,
        # the shell printed `line 12: rabbitmq: command not found`, and the
        # number-grep below took the LINE NUMBER for a version. That walked
        # straight past the "asked and could not answer" guard, because the
        # guard only recognises the word `none`. A version read off an error
        # message is not evidence of anything.
        binary = shlex.quote(command.split()[0]) if command.split() else "''"
        checks.append(f"  if ! command -v {binary} >/dev/null 2>&1; then")
        checks.append(
            f'    echo "version_{vkey}=MISSING ({command.split()[0]})"')
        checks.append("  else")
        checks.append(f"  RAW=$({command})")
        # r"" so the backslash reaches grep rather than being a Python escape.
        checks.append(r"  GOT=$(echo " '"$RAW"' r" | grep -oE '[0-9]+(\.[0-9]+)*'"
                      " | head -1)")
        if not want:
            # Reported, never compared. UNPROMISED is a distinct word rather than
            # a missing line so a human reading the report can tell "nobody
            # promised a version" from "nobody checked" — and $RAW is left out
            # deliberately: it is multi-line for several of these tools, and a
            # stray line here would be parsed as another key=value fact.
            checks.append(
                f'  echo "version_{vkey}=UNPROMISED (${{GOT:-none}})"')
            checks.append("  fi")
            continue
        checks.append("  case \"$GOT\" in")
        checks.append(f"    {want}|{want}.*) echo \"version_{vkey}=OK ($GOT)\" ;;")
        checks.append(f"    *) echo \"version_{vkey}=WRONG wanted {want} got "
                      f"${{GOT:-none}} [$RAW]\" ;;")
        checks.append("  esac")
        checks.append("  fi")
    checks.append("  echo '--- services ---'")
    for service in services:
        checks.append(f"  echo \"{service}=$(systemctl is-active {service} 2>&1)\"")
    checks.append("  echo '--- ports ---'")
    # WHAT THE SOFTWARE ACTUALLY OPENED (C7), as opposed to what the recipe
    # declared. RabbitMQ's report carried an empty ports section because the
    # drafted profile declared none — so even a successful install would have
    # certified a broker nothing could reach.
    #
    # BY DIFFERENCE, not by attribution. Matching a socket to its process is
    # unreliable exactly where it matters: RabbitMQ runs as `beam.smp`, and any
    # rule keyed on the technology name would miss it. Comparing the listening
    # sockets from before the install against after attributes nothing and
    # cannot be fooled — sshd was listening before, so 22 can never appear here.
    # POSIX only: this script runs under /bin/sh, where `<(...)` is a syntax
    # error and not a feature. A baseline that silently never compared would
    # report "no new ports" on every machine — a check that cannot fail.
    checks.append("  BEFORE=/var/lib/infra-portal/ports-before")
    checks.append("  if [ -f \"$BEFORE\" ]; then")
    checks.append(
        "    ss -lnt 2>/dev/null | awk 'NR>1 {n=split($4,a,\":\"); print a[n]}' "
        "| sort -u > /tmp/ports-now")
    checks.append(
        "    OPENED=$(comm -13 \"$BEFORE\" /tmp/ports-now 2>/dev/null | "
        "grep -E '^[0-9]+$' | paste -sd, -)")
    checks.append('    echo "opened_ports=${OPENED:-none}"')
    checks.append("  else")
    # DISTINCT FROM `none`. "Nothing new opened" and "nobody took a baseline"
    # are different facts, and reporting the second as the first is how a
    # missing measurement comes to read as a measured zero.
    checks.append('    echo "opened_ports=unknown"')
    checks.append("  fi")
    for port in ports:
        checks.append(
            f"  echo \"http_{port}=$(curl -s -o /dev/null -m 5 -w '%{{http_code}}' "
            f"http://localhost:{port}/ 2>&1)\"")
        checks.append(f"  ss -lnt 2>/dev/null | grep ':{port} ' || echo 'nothing listening on {port}'")
        # The firewall, asked DIRECTLY.
        #
        # The three checks above all pass on a machine nobody can reach: curl to
        # localhost and ss both look at the daemon, not the firewall. That is not
        # theoretical — REQ-2026-0136 reported http_80=200 and a listening socket
        # while every other machine on its subnet got "No route to host".
        #
        # Nor is testing the machine's own private IP enough: that traffic
        # arrives on lo, and the first rule in both images' rule sets accepts
        # everything on lo before any port rule is consulted. So it would pass
        # too. Only the rule set itself answers the question honestly.
        query = _FIREWALL[family]["query"].format(port=port)
        checks.append(f"  if {query} >/dev/null 2>&1; then echo 'firewall_{port}=open'; "
                      f"else echo 'firewall_{port}=CLOSED'; fi")
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


def _generated_profiles() -> dict[str, dict]:
    """Agent-written technology profiles, read from the generated store.

    A GENERATED PROFILE MAY NEVER SHADOW A SHIPPED ONE. The same rule the
    blueprint registry applies to generated manifests, for the same reason: a
    reviewed recipe that a machine can silently replace is not reviewed. nginx
    has a profile somebody checked against a real image; an agent proposing a
    different one must not be able to take its place.

    A malformed file is skipped rather than raising — one bad draft must not
    take first-boot configuration off the air for every other technology.
    """
    profiles: dict[str, dict] = {}
    try:
        entries = sorted(GENERATED_PROFILE_DIR.glob("*.json"))
    except OSError:
        return {}
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        code = str(data.get("code") or path.stem).strip()
        if not code or code in TEMPLATES:
            continue          # shipped wins, always
        # SECOND LOCK. The API's linter refuses to publish a profile with
        # problems, and this refuses to load one — because the store is a
        # directory, and a directory is not an authority. A profile that reached
        # it by any other route (a stale file, a bug, a hand edit) still does not
        # get to write root commands onto a machine.
        broken = profile_rules.profile_problems(data)
        if broken:
            _log.warning("skipping generated profile %s: %s", path.name,
                         "; ".join(broken)[:300])
            continue
        profiles[code] = data
    return profiles


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
    # Order is the authority order: what a person shipped, then what the agent
    # proposed, then what an operator set by hand to correct a live image.
    merged = {**TEMPLATES, **_generated_profiles(), **_package_overrides()}
    prof = merged.get((code or "").strip())
    if not isinstance(prof, dict):
        return None

    # Per-family block, or the dict itself when an override was written flat.
    block = prof.get(family)
    if not isinstance(block, dict):
        block = prof if "packages" in prof else None
    # An ARCHIVE install (C6b) may carry no packages at all — the software ships
    # as a tarball, not an RPM — but the family block must still exist: tar and
    # curl are universal, yet what this family was PROVEN to run is not, and
    # coverage is a per-family claim the proof makes one family at a time.
    archive = prof.get("archive") if isinstance(prof.get("archive"), dict) else None
    container = (prof.get("container")
                 if isinstance(prof.get("container"), dict) else None)
    # A CONTAINER IS AN INSTALL. Without it named here a container profile
    # declaring no packages is dropped as installing nothing, and the technology
    # silently disappears from the machine's configuration — the software is
    # never installed and nothing says why.
    if not isinstance(block, dict) or not (block.get("packages")
                                           or archive or container):
        return None

    return {"family": family,
            "archive": archive,
            "container": container,
            # A vendor repository to add BEFORE installing. `dnf install vault`
            # finds nothing on Oracle Linux because HashiCorp ships Vault from
            # its own repository — REQ-2026-0184 spent a machine learning that.
            "repo": prof.get("repo") if isinstance(prof.get("repo"), dict) else None,
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
    return (set(TEMPLATES) | set(_generated_profiles())
            | {k for k in _package_overrides() if isinstance(k, str)})


def enabled() -> bool:
    """First-boot configuration is opt-in: it changes what a VM does at boot, and
    it needs subnet egress to work at all."""
    return (os.getenv("CONFIG_ENABLED", "false").strip().lower()
            in ("1", "true", "yes", "on"))


def render(components: list[dict], family: str = "", report_url: str = "",
           preinstalled: "set[str] | frozenset[str] | tuple[str, ...]" = ()) -> str:
    """Cloud-init user-data configuring the request's technologies, or "" when
    there is nothing to configure (or the feature is off).

    `family` is the OS family of the image THIS machine boots, resolved by the
    caller from the requester's chosen image. It defaults to CONFIG_OS_FAMILY,
    which is what every request that names no image gets — unchanged behaviour.

    A technology with no recipe for this family is SKIPPED, not guessed at.
    Rendering `dnf install httpd` onto an Ubuntu machine produces a boot that
    completes, writes a success marker, and installs nothing: the exact silent
    success this module was rewritten to stop.

    `preinstalled` names technologies whose software is ALREADY ON THE IMAGE,
    because a golden image captured from a proven machine is booting (G2). Their
    install steps are skipped — and nothing else is. The report still runs the
    same version command against the same ports, and `packages` still carries
    them so the machine is still asked whether they are present.

    That distinction is the whole safety of the feature: skipping the INSTALL is
    a speed-up, skipping the VERIFICATION would mean a badly captured image
    shipped a broken runtime to everyone who asked for it, silently.

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
    if not profiles and not report_url:
        return ""
    # A machine with NOTHING to install still has something to prove.
    #
    # This used to return "" here, so a compute-vm or rhel9 request booted with no
    # cloud-init at all and filed no report — which made those blueprints
    # unprovable BY CONSTRUCTION. The certification gate refused them for lack of
    # evidence they could never produce, and no amount of testing would have
    # changed that.
    #
    # A bare VM's job is to exist, so the three things worth proving about it are
    # that the image boots, that cloud-init ran, and that the machine can reach
    # Object Storage — which a report saying nothing else still demonstrates,
    # because it arrived.

    packages: list[str] = []
    services: list[str] = []
    ports: list[int] = []
    modules: list[str] = []
    # (code, archive spec) for software that ships as a tarball, not a package
    # (C6b). Its dependencies still ride the packages list — java for keycloak is
    # a real RPM — but the software itself is fetched, checked, and unpacked.
    archives: list[tuple[str, dict]] = []
    repos: list[tuple[str, dict]] = []
    containers: list[tuple[str, dict]] = []
    # Packages that arrived ON THE IMAGE. Held separately rather than filtered
    # out of `packages`, because that list answers TWO questions — what to
    # install, and what the report asks the machine about. Removing them would
    # have silently stopped verifying the very software the image exists to
    # provide.
    skip_packages: set[str] = set()
    already: set[str] = {str(c) for c in (preinstalled or ())}

    for code, version, prof in profiles:
        on_image = code in already
        if on_image:
            skip_packages.update(prof["packages"])
        if prof.get("archive") and not on_image:
            archives.append((code, prof["archive"]))
        if prof.get("repo") and not on_image:
            repos.append((code, prof["repo"]))
        if prof.get("container"):
            containers.append((code, prof["container"]))
            # SUPPLIED HERE, not declared in the profile. What runs a container
            # is a property of this renderer's choice of runtime, not of the
            # technology — and a profile that had to remember it would produce a
            # machine that pulls nothing, starts nothing, and says only that the
            # service is inactive.
            if "podman" not in packages:
                packages.append("podman")
        stream = "" if on_image else module_stream(code, version, family)
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
    # Archive software has no package to carry a systemd unit, so the profile
    # declares one and it is written here — as a file, for the same reason the
    # report script is: unit syntax must never have to survive YAML quoting.
    for code, spec in archives:
        unit = spec.get("unit") or {}
        if not unit:
            continue
        lines.append(f"  - path: /etc/systemd/system/{code}.service")
        lines.append("    permissions: '0644'")
        lines.append("    content: |")
        lines.append("      [Unit]")
        lines.append(f"      Description={unit.get('description') or code} (installed by the provisioning portal)")
        lines.append("      After=network-online.target")
        lines.append("      Wants=network-online.target")
        lines.append("      [Service]")
        if spec.get("user"):
            lines.append(f"      User={spec['user']}")
        for key, value in (unit.get("environment") or {}).items():
            lines.append(f"      Environment={key}={value}")
        lines.append(f"      ExecStart={unit.get('exec_start', '')}")
        lines.append("      Restart=on-failure")
        lines.append("      [Install]")
        lines.append("      WantedBy=multi-user.target")
    # --- Quadlet units, one per container (C8) -------------------------------
    #
    # WRITTEN AS A FILE, for the same reason the report script is: an ini file
    # full of colons and slashes does not have to survive YAML quoting, and
    # systemd reads it directly. Quadlet converts it into a real unit at
    # daemon-reload, so `systemctl is-active <code>` — the check every other
    # technology already faces — works unchanged.
    #
    # EVERY VALUE HERE CAME THROUGH profile_rules.container_problems(). The
    # profile contributes data, never flags: there is no field it can set that
    # becomes a podman argument this renderer did not choose.
    for code, spec in containers:
        lines.append(f"  - path: /etc/containers/systemd/{code}.container")
        lines.append("    permissions: '0644'")
        lines.append("    content: |")
        lines.append("      [Unit]")
        lines.append(f"      Description={code}, run by the provisioning portal")
        lines.append("      [Container]")
        # PINNED BY DIGEST. The tag, if any, is a comment for a human reading
        # the machine — what is actually pulled is the digest, so a publisher
        # repointing the tag cannot change what a proved recipe installs.
        lines.append(f"      Image={spec['image']}@{spec['digest']}")
        if spec.get("tag"):
            lines.append(f"      # tag at the time of drafting: {spec['tag']}")
        lines.append(f"      ContainerName={code}")
        for port in (profile_for(code, family) or {}).get("ports", []):
            lines.append(f"      PublishPort={int(port)}:{int(port)}")
        if spec.get("data_dir") and spec.get("data_mount"):
            # :Z relabels for SELinux, which is enforcing on Oracle Linux. Without
            # it the container is denied access to its own data directory and the
            # service fails for a reason that looks nothing like a mount problem.
            lines.append(
                f"      Volume={spec['data_dir']}:{spec['data_mount']}:Z")
        for env_key, env_value in sorted((spec.get("environment") or {}).items()):
            lines.append(f"      Environment={env_key}={env_value}")
        lines.append("      [Service]")
        lines.append("      Restart=always")
        lines.append("      [Install]")
        # What makes it come back after a reboot. A generated unit cannot be
        # `systemctl enable`d, so this section is the only thing that installs it.
        lines.append("      WantedBy=multi-user.target")

    # The machine's own self-check, written as a FILE so its quotes and loops
    # never have to survive YAML quoting — the failure that has already cost this
    # project two machines.
    if report_url:
        lines.append("  - path: /usr/local/bin/infra-portal-report.sh")
        lines.append("    permissions: '0755'")
        lines.append("    content: |")
        for check in _report_script(
                [(code, version) for code, version, _profile in profiles],
                packages, services, ports, family, report_url,
                archives=archives, repos=repos, containers=containers,
                # ONLY FOR A PACKAGE GUESS. A profile carrying an archive or
                # a vendor repository is not guessing a name — keycloak IS a
                # tarball, and searching the repositories for it finds nothing,
                # installs EPEL for no reason, and reports a `none` that means
                # only "this was never a package". Discovery answers the
                # question "is this packaged under another name", and that
                # question is only open for a guess.
                candidates={
                    code: (prof["packages"][0], candidate_packages(code))
                    for code, _v, prof in profiles
                    if not prof.get("archive") and not prof.get("repo")
                    and prof.get("packages") and candidate_packages(code)}):
            lines.append(f"      {check}")
    lines.append("runcmd:")
    # Module streams are enabled BEFORE the install, or the default stream is
    # already resolved and the wrong major version comes down.
    for mod in modules:
        lines.append(cmd(f"{_MODULE_ENABLE[family]} {mod} || echo 'PORTAL FAILURE: could not "
                         f"enable module {mod}' >> /var/log/infra-portal.log"))
    # VENDOR REPOSITORIES, before any package install — the whole point is that
    # the packages below do not exist until these are added.
    #
    # The GPG key is imported explicitly rather than left to dnf's prompt: a
    # non-interactive boot would otherwise either hang or silently install
    # unverified packages, and the second is worse. gpgcheck is enforced in the
    # rules, so a repo reaching here has a key.
    # The listening-socket baseline, taken BEFORE anything is installed so the
    # report can attribute new ports to the software by difference (C7).
    lines.append(cmd("mkdir -p /var/lib/infra-portal"))
    lines.append(cmd(
        "ss -lnt 2>/dev/null | awk 'NR>1 {n=split($4,a,\":\"); print a[n]}' "
        "| sort -u > /var/lib/infra-portal/ports-before || true"))
    for code, spec in repos:
        # A repository that arrives as a PACKAGE rather than a URL (C7). EPEL is
        # enabled this way, and the release package carries the repository
        # definition and its signing key together — so there is no separate
        # rpm --import step and nothing to quote but a name that
        # profile_rules.RELEASE_PACKAGES has already closed to two values.
        release = spec.get("release_package")
        if release:
            lines.append(cmd(
                f"{install} {shlex.quote(release)} || echo 'PORTAL FAILURE: "
                f"could not enable the {code} repository' "
                f">> /var/log/infra-portal.log"))
            continue
        url = shlex.quote(spec.get("url", ""))
        key = shlex.quote(spec.get("gpg_key", ""))
        # WHAT THESE TOOLS ACTUALLY SAID, when they fail.
        #
        # The same defect the package install had until this morning, sitting
        # six lines above it. REQ-2026-0205 is what it costs: the machine could
        # only report "could not add the mongodb repository", and finding out it
        # was a 404 took a person and an afternoon. Both commands now copy their
        # own last lines into the report, prefixed so their prose can never be
        # mistaken for one of the report's key=value facts.
        lines.append(cmd(
            f"rpm --import {key} > /tmp/portal-repo.log 2>&1 || "
            f"{{ echo 'PORTAL FAILURE: could not import the signing key for "
            f"{code}' >> /var/log/infra-portal.log; "
            f"tail -4 /tmp/portal-repo.log | sed 's/^/  repo: /' "
            f">> /var/log/infra-portal.log; }}"))
        lines.append(cmd(
            f"{_REPO_ADD[family]} {url} > /tmp/portal-repo.log 2>&1 || "
            f"{{ echo 'PORTAL FAILURE: could not add the {code} repository' "
            f">> /var/log/infra-portal.log; "
            f"tail -4 /tmp/portal-repo.log | sed 's/^/  repo: /' "
            f">> /var/log/infra-portal.log; }}"))
    if repos:
        lines.append(cmd("dnf clean all >/dev/null 2>&1 || true"))

    # WHAT STILL HAS TO BE INSTALLED. `packages` keeps every name for the report;
    # this is the subset the machine must actually fetch.
    to_install = [p for p in packages if p not in skip_packages]
    if skip_packages:
        lines.append(cmd(
            f"echo 'preinstalled: {' '.join(sorted(skip_packages))}' "
            f">> /var/log/infra-portal.log"))

    if to_install:
        # `|| true` keeps a failed install from aborting the rest of cloud-init, so
        # the marker + log survive for diagnosis instead of a silent dead VM.
        # WHAT THE PACKAGE MANAGER ACTUALLY SAID, when it fails.
        #
        # This was `dnf install ... || echo 'PORTAL FAILURE'`, so dnf's own
        # explanation went to cloud-init's log and never reached the report.
        # REQ-2026-0203 is what that costs: the search found `dotnet8.0`,
        # repoquery listed it in ol9_appstream, `dnf install dotnet8.0` refused
        # it — and the machine could only say "did not complete". dnf had said
        # why, in a sentence, and we threw it away.
        #
        # Bounded to the last eight lines and prefixed, so a package manager's
        # prose cannot be mistaken for the report's own key=value facts.
        lines.append(cmd(
            f"{install} {' '.join(to_install)} > /tmp/portal-install.log 2>&1 || "
            f"{{ echo 'PORTAL FAILURE: package install did not complete' "
            f">> /var/log/infra-portal.log; "
            f"tail -8 /tmp/portal-install.log | sed 's/^/  install: /' "
            f">> /var/log/infra-portal.log; }}"))
    # ARCHIVE INSTALLS, after the dependency packages and before the services
    # that depend on them. Every step leaves a PORTAL FAILURE marker on the
    # machine when it fails, because the marker is what the boot report carries
    # and the machine's own words are what a failed proof gets judged on.
    for code, spec in archives:
        # THIRD LOCK: shell-quoted, even though profile_rules has already refused
        # anything containing a shell metacharacter. Two locks on one door is the
        # rule everywhere else in this system (the publish path, the proof
        # authority), and the cost here is one function call.
        url = shlex.quote(spec.get("url", ""))
        dest_raw = spec.get("dest", f"/opt/{code}")
        dest = shlex.quote(dest_raw)
        tmp = shlex.quote(f"/tmp/portal-archive-{code}.tgz")
        lines.append(cmd(
            f"curl -fsSL -o {tmp} {url} || echo 'PORTAL FAILURE: archive fetch "
            f"failed for {code}' >> /var/log/infra-portal.log"))
        if spec.get("sha256"):
            # A MISMATCHED ARCHIVE IS DELETED, not merely reported. Root is about
            # to execute what is inside it, and the marker alone would not stop
            # the unpack step that follows.
            sha = shlex.quote(spec["sha256"])
            lines.append(cmd(
                f"echo {sha}\"  \"{tmp} | sha256sum -c - || {{ echo "
                f"'PORTAL FAILURE: archive checksum mismatch for {code}' >> "
                f"/var/log/infra-portal.log; rm -f {tmp}; }}"))
        # UNPACK, THEN PROVE IT UNPACKED SOMETHING.
        #
        # `mkdir -p {dest} && tar ...` as one command created the directory the
        # report then tested for, so archive_<code> could never say `failed` —
        # a check that cannot fail, which is the third time in one day this
        # project has shipped one. Worse, `tar --strip-components=1` on an
        # archive whose members sit at the top level extracts NOTHING and exits
        # 0, so even a real tar failure was not guaranteed to be noticed.
        #
        # So the unpack is judged by what is on disk afterwards, and the marker
        # is written when the destination is empty however tar exited.
        lines.append(cmd(f"mkdir -p {dest}"))
        lines.append(cmd(
            f"tar -xzf {tmp} -C {dest} --strip-components=1 "
            f"|| echo 'PORTAL FAILURE: archive unpack failed for {code}' >> "
            f"/var/log/infra-portal.log"))
        lines.append(cmd(
            f"[ -n \"$(ls -A {dest} 2>/dev/null)\" ] || echo 'PORTAL FAILURE: "
            f"archive for {code} unpacked no files' >> /var/log/infra-portal.log"))
        if spec.get("user"):
            user = shlex.quote(spec["user"])
            lines.append(cmd(
                f"useradd -r -s /sbin/nologin {user} 2>/dev/null; "
                f"chown -R {user}:{user} {dest} || echo 'PORTAL "
                f"FAILURE: could not chown {dest_raw}' >> /var/log/infra-portal.log"))
        lines.append(cmd(f"rm -f {tmp}"))
    # --- service data, on its own block volume (C8) --------------------------
    #
    # MOUNTED BEFORE THE CONTAINER STARTS, or the container creates its data
    # directory on the boot volume, writes there happily, and the machine looks
    # perfectly healthy right up until somebody detaches the volume that was
    # supposed to hold it and finds it empty.
    for code, spec in containers:
        data_dir = spec.get("data_dir")
        if not data_dir:
            continue
        quoted = shlex.quote(data_dir)
        key = profile_rules.report_key(code)
        # THE DEVICE IS FOUND, NOT ASSUMED. A paravirtualized attachment appears
        # under /dev/oracleoci when oci-utils is present and as a plain /dev/sdX
        # when it is not, and the kernel is free to order those differently
        # between boots. Each candidate is tested for existence rather than
        # hoped for.
        lines.append(cmd(
            "for C in /dev/oracleoci/oraclevdb /dev/sdb /dev/nvme1n1; do "
            "[ -b \"$C\" ] && DATADEV=\"$C\" && break; done; "
            "echo \"${DATADEV:-none}\" > /var/lib/infra-portal-datadev"))
        # NEVER mkfs A DEVICE THAT ALREADY HAS A FILESYSTEM. This script runs at
        # every first boot, and a rebuilt machine reattached to an existing
        # volume must find its data, not lose it.
        lines.append(cmd(
            "DATADEV=$(cat /var/lib/infra-portal-datadev); "
            "if [ \"$DATADEV\" != none ]; then "
            "blkid \"$DATADEV\" >/dev/null 2>&1 || "
            "mkfs.xfs -q -L portaldata \"$DATADEV\"; fi"))
        lines.append(cmd(f"mkdir -p {quoted}"))
        # BY UUID, not by device name: /dev/sdb is not a stable identity across
        # reboots, and an fstab entry pointing at the wrong disk is worse than
        # none. `nofail` so a volume that is slow to attach cannot leave the
        # machine unbootable and unreachable.
        lines.append(cmd(
            "DATADEV=$(cat /var/lib/infra-portal-datadev); "
            "if [ \"$DATADEV\" != none ]; then "
            "DUUID=$(blkid -s UUID -o value \"$DATADEV\"); "
            f"grep -q \"$DUUID\" /etc/fstab || "
            f"echo \"UUID=$DUUID {data_dir} xfs defaults,_netdev,nofail 0 2\" "
            ">> /etc/fstab; fi"))
        lines.append(cmd(
            f"mountpoint -q {quoted} || mount {quoted} || echo 'PORTAL FAILURE: "
            f"the data volume did not mount at {data_dir}' "
            f">> /var/log/infra-portal.log"))
        _ = key

    # --- containers ----------------------------------------------------------
    for code, spec in containers:
        image = spec["image"] + "@" + spec["digest"]
        lines.append(cmd(
            f"podman pull {shlex.quote(image)} || echo 'PORTAL FAILURE: could "
            f"not pull the {code} image' >> /var/log/infra-portal.log"))

    if archives:
        lines.append(cmd("systemctl daemon-reload || true"))
    if containers:
        # Quadlet turns /etc/containers/systemd/<code>.container into a real
        # systemd unit at daemon-reload. The unit is GENERATED, so it is started
        # rather than enabled — `systemctl enable` on a generated unit fails, and
        # the [Install] section written into the .container file is what makes it
        # come back after a reboot.
        lines.append(cmd("systemctl daemon-reload || true"))
    for svc in services:
        if any(svc == code for code, _spec in containers):
            lines.append(cmd(
                f"systemctl start {svc} || echo 'PORTAL FAILURE: {svc} did not "
                f"start' >> /var/log/infra-portal.log"))
            continue
        lines.append(cmd(f"systemctl enable --now {svc} || echo 'PORTAL FAILURE: {svc} did not start' >> /var/log/infra-portal.log"))
    # The OS firewall, from the same declaration that drives the network rules.
    # Without this a service starts correctly and is still unreachable — which
    # looks exactly like a broken install.
    for port in ports:
        add = firewall["add"].format(port=port)
        lines.append(cmd(f"{add} || echo 'PORTAL FAILURE: could not open {port}/tcp in the firewall' >> /var/log/infra-portal.log"))
    if ports:
        lines.append(cmd(f"{firewall['reload']} || true"))
    # Say on the machine what was skipped and why, so a missing service is
    # diagnosable from the VM without going back to the portal.
    for code in unsupported:
        lines.append(cmd(f"echo 'PORTAL FAILURE: {code} has no install recipe for {family}; "
                         f"nothing was installed for it' >> /var/log/infra-portal.log"))
    lines.append(cmd("echo 'PORTAL: first-boot configuration finished' >> /var/log/infra-portal.log"))
    # LAST, so the report describes the finished machine rather than one still
    # installing. Its failure is swallowed: a machine that works but could not
    # upload its report is a better outcome than one that fails boot over an
    # unreachable bucket.
    if report_url:
        # Archive software can take minutes to answer its port after the unit is
        # active — keycloak builds itself on first start — and a report filed the
        # instant enable returned would call a healthy machine broken on the port
        # check. Bounded: a service that never answers still fails, five minutes
        # later, on the same evidence.
        # `archives or repos`, not archives alone. A service installed from a
        # vendor repository is just as capable of taking a minute to bind — and
        # reporting the instant `systemctl enable` returns would call it broken
        # on the port check, which is this whole file's recurring failure.
        # `containers` too: an image has to be pulled, unpacked and started
        # before anything listens, which is the slowest of the three. Reporting
        # the instant `systemctl start` returns would call a healthy machine
        # broken on the port check — this file's recurring failure.
        if archives or repos or containers:
            for port in ports:
                # WAITS FOR THE SOCKET, NOT FOR HTTP. This used to curl the port
                # and break on success — which never succeeds for a service that
                # does not speak HTTP, so it waited the full five minutes on
                # each. RabbitMQ publishes AMQP on 5672, epmd on 4369 and
                # clustering on 25672, none of them HTTP: four ports meant
                # twenty minutes of waiting before the report was even written,
                # and REQ-2026-0193's narrowing proof timed out at 2202s with
                # "still waiting for oci-service-vm to report".
                #
                # A listening socket is what "the service is up" actually means,
                # and `ss` answers it for any protocol.
                lines.append(cmd(
                    f"for i in $(seq 1 60); do "
                    f"ss -lnt 2>/dev/null | grep -q ':{port} ' && break; "
                    f"sleep 5; done"))
        lines.append(cmd("/usr/local/bin/infra-portal-report.sh || true"))
    return "\n".join(lines) + "\n"


def streams_for(code: str, family: str) -> list[str]:
    """Module streams this OS family actually offers for a technology.

    Empty means either the family pins no versions — Debian carries whatever its
    release holds — or nobody has measured this one. Either way the caller must
    not then offer a version list it invented, which is exactly how the form came
    to offer nginx 1.20 on an Oracle Linux 9.8 that has no such stream.
    """
    profile = (TEMPLATES.get((code or "").strip(), {})
               .get((family or "").strip().lower(), {}))
    return list(profile.get("streams") or [])
