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
from db.models import Technology

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
    # A versioned code with no sibling is still software on a machine
    # (postgres16 was; kafka4 would be), not a cloud-managed service.
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
    if not families:
        block("no-family",
              "The profile covers no OS family. Package and service names differ per "
              "family — Apache is `httpd` on Oracle Linux and `apache2` on Ubuntu — so "
              "a profile that names neither cannot install anything.")

    for family in families:
        blk = profile[family]
        if not blk.get("packages"):
            block("no-packages", f"The {family} block installs no package.")
        if not blk.get("services"):
            findings.append(Finding(
                "warning", "no-service",
                f"The {family} block enables no systemd unit, so the software would be "
                f"installed but not running."))

    if not str(profile.get("version_command") or "").strip():
        block("no-version-command",
              "The profile gives no way to ask the machine what it actually received. "
              "A package called `redis` delivered Redis 6.2 on Oracle Linux while the "
              "catalogue promised Redis 7, and nothing noticed until somebody logged "
              "in. A recipe that cannot be checked cannot be certified.")

    if code and code in shipped_codes:
        block("shadows-shipped",
              f"`{code}` already has a reviewed profile. A generated one may not take "
              f"its place — the same rule the blueprint registry applies to manifests.")

    return findings


def draft_profile(candidate: str, target: str = "oci") -> Draft:
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
    profile = {
        "code": code,
        "builds_on": "oci/service-vm" if target == "oci" else "",
        "ports": [],
        "version_command": f"{code} --version 2>&1",
        "rhel": {"packages": [code], "services": [code]},
        "_note": ("DRAFT — proposed by the agent, proven by booting a machine. "
                  "The package name is the obvious guess and may not exist in the "
                  "image's repositories; the machine's own report decides."),
    }
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
          shipped_codes: frozenset[str] = frozenset()) -> Draft:
    """Propose a recipe for one catalogue candidate. Never applies anything."""
    candidate = (candidate or "").strip()
    if not candidate:
        raise ValueError("No candidate given.")

    kind, sibling = classify(candidate, session)
    if kind == "version-bump" and sibling is not None:
        proposal = _bump_draft(candidate, sibling)
    elif kind == "vm-service":
        proposal = draft_profile(candidate, target)
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
