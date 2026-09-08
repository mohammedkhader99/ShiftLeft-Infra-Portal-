"""Which existing clusters a requester may deploy onto, and which of them fit.

Scenario B of the placement step (P.5, F-IAM-11). A requester selecting OKE or
AKS is offered either an existing cluster or a new one; this module answers the
first half — what "existing" means for *this* caller.

TWO DIFFERENT QUESTIONS, DELIBERATELY ANSWERED DIFFERENTLY.

  Entitlement is a VISIBILITY boundary. A cluster outside the requester's
  project, cost centre and subsidiary is not returned at all — not greyed, not
  listed with a reason. The placement task asks that every rejected option show
  why, and that is right for capacity and quota; applying it to entitlement
  would publish the name, region and size of another department's
  infrastructure to anyone who opens the request form. You cannot be refused
  something you were never offered, and "cluster prod-egate-01 exists but is not
  yours" is a sentence worth more to an attacker than to a requester.

  Capacity and quota are ELIGIBILITY within what you can already see. Those come
  back listed and ineligible, with the number that stopped them, because the
  requester can act on that: ask for less, or ask for the ceiling to be raised.

NEVER TRUST A CLUSTER ID FROM THE BROWSER. `authorise_cluster` exists for the
API to re-resolve an id the client claims, against the caller's real scope,
before anything is persisted. A list rendered for display is a convenience; the
authorisation is the control, and it happens again server-side (ARCHITECTURE.md
P1/P2). The browser holds no authority — a cluster id in a POST body is an
assertion, not a fact.

NO I/O. Cluster facts arrive as arguments. They are FETCHED from the cloud and
cached briefly, never stored as catalogue rows: a constant in this repository
describing what a cloud currently offers is a defect waiting for a date
(ARCHITECTURE.md P7), and allocatable capacity is the most perishable fact here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# Workloads that keep data on the cluster. Kubernetes runs them; the operational
# burden of storage, failover and backup moves to whoever asked for it.
STATEFUL_COMPONENTS = frozenset({"postgres16", "mysql", "mssql", "oracle-free",
                                 "mongodb", "valkey", "opensearch"})


@dataclass(frozen=True)
class Cluster:
    """A cluster as the cloud currently reports it."""

    id: str
    name: str
    region: str
    project_code: str | None = None
    cost_centre_code: str | None = None
    subsidiary_code: str | None = None
    allocatable_vcpu: int = 0
    allocatable_memory_gb: int = 0


@dataclass(frozen=True)
class RequesterScope:
    """What this caller may see, resolved from their identity — never from the
    request body."""

    project_codes: frozenset[str] = frozenset()
    cost_centre_codes: frozenset[str] = frozenset()
    subsidiary_codes: frozenset[str] = frozenset()

    def covers(self, cluster: Cluster) -> bool:
        """A cluster is visible when every scope it declares is one the caller
        holds. An unscoped cluster (no project, no cost centre, no subsidiary)
        is shared infrastructure and visible to all."""
        for value, held in (
            (cluster.project_code, self.project_codes),
            (cluster.cost_centre_code, self.cost_centre_codes),
            (cluster.subsidiary_code, self.subsidiary_codes),
        ):
            if value is not None and value not in held:
                return False
        return True


@dataclass(frozen=True)
class Need:
    """What the request would consume on whichever cluster takes it."""

    vcpu: int = 0
    memory_gb: int = 0


@dataclass(frozen=True)
class Quota:
    """A ceiling this request must not push past (F-FIN-08)."""

    label: str
    limit: int
    used: int
    unit: str = "vCPU"

    @property
    def remaining(self) -> int:
        return self.limit - self.used


@dataclass(frozen=True)
class ClusterOption:
    cluster: Cluster
    eligible: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "id": self.cluster.id,
            "name": self.cluster.name,
            "region": self.cluster.region,
            "allocatable_vcpu": self.cluster.allocatable_vcpu,
            "allocatable_memory_gb": self.cluster.allocatable_memory_gb,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
        }


def visible_clusters(clusters: Iterable[Cluster],
                     scope: RequesterScope) -> list[Cluster]:
    """Only what this caller is entitled to see. See the module docstring for
    why the rest are omitted rather than listed as refused."""
    return [c for c in clusters if scope.covers(c)]


def _capacity_reasons(cluster: Cluster, need: Need) -> list[str]:
    reasons = []
    if need.vcpu > cluster.allocatable_vcpu:
        reasons.append(
            f"Needs {need.vcpu} vCPU; {cluster.name} has "
            f"{cluster.allocatable_vcpu} allocatable.")
    if need.memory_gb > cluster.allocatable_memory_gb:
        reasons.append(
            f"Needs {need.memory_gb} GB of memory; {cluster.name} has "
            f"{cluster.allocatable_memory_gb} GB allocatable.")
    return reasons


def _quota_reasons(need: Need, quotas: Sequence[Quota]) -> list[str]:
    reasons = []
    for quota in quotas:
        requested = need.vcpu if quota.unit == "vCPU" else need.memory_gb
        if requested > quota.remaining:
            reasons.append(
                f"{quota.label} would be exceeded: {quota.used} of "
                f"{quota.limit} {quota.unit} already committed, "
                f"{quota.remaining} left, this request needs {requested}.")
    return reasons


def assess_clusters(
    clusters: Iterable[Cluster],
    scope: RequesterScope,
    need: Need,
    quotas: Sequence[Quota] = (),
) -> list[ClusterOption]:
    """Every cluster this caller may see, each marked eligible or refused.

    Refusals name the number that caused them — the allocatable capacity, or the
    ceiling and what is left of it. "Ineligible" on its own tells a requester
    nothing they can act on, and produces the ticket this step exists to avoid.
    """
    options = []
    for cluster in visible_clusters(clusters, scope):
        reasons = _capacity_reasons(cluster, need) + _quota_reasons(need, quotas)
        options.append(ClusterOption(
            cluster=cluster, eligible=not reasons, reasons=tuple(reasons)))
    return options


def authorise_cluster(
    cluster_id: str,
    clusters: Iterable[Cluster],
    scope: RequesterScope,
    need: Need,
    quotas: Sequence[Quota] = (),
) -> ClusterOption:
    """Re-resolve a cluster id the client supplied, against the caller's scope.

    Raises rather than returning a refusal, because the caller of this function
    is about to persist a placement: a return value can be ignored by mistake,
    an exception cannot. An id outside the caller's entitlement is reported as
    "no such cluster" — the same answer as an id that does not exist, so the API
    cannot be used to discover which clusters are real.
    """
    for option in assess_clusters(clusters, scope, need, quotas):
        if option.cluster.id == cluster_id:
            if not option.eligible:
                raise ClusterRefused("; ".join(option.reasons))
            return option
    raise ClusterRefused(
        f"No cluster '{cluster_id}' is available to you.")


class ClusterRefused(PermissionError):
    """The claimed cluster is not one this caller may deploy onto."""


def stateful_warnings(components: Iterable[str],
                      managed_available: Iterable[str] = ()) -> list[str]:
    """Advice for putting data-keeping workloads on Kubernetes.

    Warns rather than blocks: running PostgreSQL on a cluster is a legitimate
    choice, and the portal's job is to make sure it is a choice made knowingly
    rather than by default. Where the same component exists as a managed
    service, that is named — the alternative is more useful than the caution.
    """
    managed = set(managed_available)
    warnings = []
    for component in components:
        if component not in STATEFUL_COMPONENTS:
            continue
        message = (
            f"{component} keeps data. On Kubernetes, storage, failover and "
            f"backups become your team's responsibility rather than the "
            f"platform's.")
        if component in managed:
            message += (
                f" The cloud's managed {component} carries none of that; it is "
                f"offered above as 'Managed where available'.")
        warnings.append(message)
    return warnings
