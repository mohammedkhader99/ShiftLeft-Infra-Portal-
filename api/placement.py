"""Which host topologies a selection could run on, and which of them are allowed.

WHAT THIS SOLVES. A requester picks "VM with OS", "PostgreSQL" and "Node.js" and
the request goes straight to costing. Nothing has decided whether that is one
machine or three — so the estimate cannot be right, the Terraform plan has no
topology to select modules against, and the approver in Jira reads a shopping
list rather than an architecture.

This module turns a selection into the ordered options of Scenario A:

    1. managed where available   the cloud runs what it can; you patch less
    2. consolidated              one machine carries everything
    3. separated                 one machine each

WHY IT DECIDES NOTHING. It enumerates and asks; it never judges. Whether an
option is permitted is answered by OPA (`infra.placement`, P.3), because the
policy is the authority and app code that reimplements a rule is a second
authority that will one day disagree with the first.

WHY A DENIED OPTION IS STILL RETURNED. It comes back marked ineligible with the
policy's own sentence attached, never dropped. An option that vanishes without
explanation makes the portal look broken and produces the support ticket the
portal exists to prevent — so "why can't I pick that one?" is answered on the
screen where the question is asked.

NO I/O. Everything this needs arrives as arguments, including the policy
evaluator. That keeps it testable without a database or a running OPA, and it
keeps the catalogue lookups in one place (the API layer) rather than scattered
through the enumeration.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace

# Host modes, mirroring db.models. Repeated as literals rather than imported so
# this module stays free of the database layer and can be reasoned about alone.
HOST_VM = "vm"
HOST_CONTAINER = "container"
HOST_MANAGED = "managed"

MANAGED_OPTION = "managed"
CONSOLIDATED_OPTION = "consolidated"
SEPARATED_OPTION = "separated"
EXISTING_CLUSTER_OPTION = "existing-cluster"
NEW_CLUSTER_OPTION = "new-cluster"

# THE TWO QUESTIONS A REQUESTER IS ACTUALLY ASKING, and the only two that need
# answering: does this run on machines, or on Kubernetes? Every layout below is
# one answer or the other.
#
# Named HERE, beside the keys, because this is where the keys are defined. The
# browser groups layouts by this rather than by matching key strings of its own —
# a copy of that list in the client is one that goes stale the day a layout is
# added, and the requester is the one who finds out.
MACHINES_ROUTE = "machines"
KUBERNETES_ROUTE = "kubernetes"

CLUSTER_OPTIONS = frozenset({EXISTING_CLUSTER_OPTION, NEW_CLUSTER_OPTION})

# The order the requester sees them in. Managed first because it is the option
# that removes work from them; separated last because it is the most expensive.
OPTION_ORDER = (MANAGED_OPTION, CONSOLIDATED_OPTION, SEPARATED_OPTION)

# The name each layout goes by, in one place.
#
# Only the KEY is persisted with a placement, so anything reading a stored
# placement back -- the approval ticket, most of all -- has to turn "consolidated"
# into something a person can judge. Written here rather than wherever it is
# needed: an approver and a requester describing the same layout differently is
# the kind of drift nobody notices until the two are read side by side.
OPTION_TITLES: dict[str, str] = {
    MANAGED_OPTION: "Managed where available",
    CONSOLIDATED_OPTION: "Consolidated",
    SEPARATED_OPTION: "Separated",
    EXISTING_CLUSTER_OPTION: "Deploy onto an existing cluster",
    NEW_CLUSTER_OPTION: "Provision a new cluster",
}


def option_title(key: str) -> str:
    """The human name for a layout key, falling back to the key itself.

    An unknown key is returned as-is rather than replaced with "Unknown": a
    ticket naming a layout the code no longer recognises is still telling the
    truth about what was chosen, and hiding it would not make it recognised.
    """
    return OPTION_TITLES.get(key, key)


@dataclass(frozen=True)
class ComponentFacts:
    """What the catalogue knows about one selected component, on one cloud.

    `is_host` marks a component that IS a machine rather than something placed
    on one — "VM with OS" is the host its neighbours land on, not a workload
    competing for space on someone else's.
    """

    code: str
    host_modes: frozenset[str]
    is_host: bool = False
    # True for OKE/AKS: the component IS a cluster, so workloads land ON it
    # rather than beside it. Determined from the BLUEPRINT's resource kind, never
    # from Technology.resource_kind, whose own docstring says it "exists and is
    # wrong for most components".
    provides_cluster: bool = False

    def supports(self, mode: str) -> bool:
        return mode in self.host_modes

    @property
    def hosts_others(self) -> bool:
        """A machine or a cluster: something other components are placed on."""
        return self.is_host or self.provides_cluster


@dataclass(frozen=True)
class Host:
    id: str
    host_mode: str
    components: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"id": self.id, "host_mode": self.host_mode,
                "components": list(self.components)}


@dataclass(frozen=True)
class Option:
    """One way the selection could be laid out, and whether it is permitted."""

    key: str
    title: str
    summary: str
    hosts: tuple[Host, ...]
    eligible: bool = True
    reasons: tuple[str, ...] = field(default_factory=tuple)
    # Clusters this option could land on, each already assessed for entitlement,
    # capacity and quota by api.clusters. Only "existing-cluster" carries any.
    clusters: tuple = field(default_factory=tuple)
    # Advisory, never blocking: running a database on Kubernetes is a legitimate
    # choice, and the portal's job is to see it made knowingly.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def host_count(self) -> int:
        """Machines this option would provision.

        A managed service is not one, and neither is a container: a pod runs on
        a cluster, and nothing is created for it to sit on. This counted both
        and reported "2 machines" for two pods -- see `container_count` in
        api.sizing, which this has to agree with or the same layout is described
        two ways on one screen.
        """
        return sum(1 for h in self.hosts if h.host_mode == HOST_VM)

    @property
    def container_count(self) -> int:
        """Workloads this option would run as containers."""
        return sum(1 for h in self.hosts if h.host_mode == HOST_CONTAINER)

    @property
    def route(self) -> str:
        """Which of the two questions this layout answers: machines, or Kubernetes.

        The requester chooses a ROUTE; the layouts within it are the platform's
        business. Five cards arguing with each other, each with its own refusal
        and its own diagram, is the platform thinking out loud at somebody who
        asked a simpler question than that.
        """
        return KUBERNETES_ROUTE if self.key in CLUSTER_OPTIONS else MACHINES_ROUTE

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "route": self.route,
            "title": self.title,
            "summary": self.summary,
            "hosts": [h.as_dict() for h in self.hosts],
            "host_count": self.host_count,
            "container_count": self.container_count,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "clusters": [c.as_dict() if hasattr(c, "as_dict") else c
                         for c in self.clusters],
            "warnings": list(self.warnings),
        }


def topology_document(option: Option, environment: str,
                      deployment_target: str) -> dict:
    """The shape `infra.placement` expects as its input."""
    return {
        "environment": environment,
        "deployment_target": deployment_target,
        "hosts": [h.as_dict() for h in option.hosts],
    }


def _workloads(components: Sequence[ComponentFacts]) -> list[ComponentFacts]:
    """The components that need placing, as opposed to the things they land on."""
    return [c for c in components if not c.hosts_others]


def resolved_facts(components: Sequence[ComponentFacts]) -> list[ComponentFacts]:
    """The components as every BUILDABLE layout treats them.

    A cluster is only hosting something if the portal can put something on it,
    and it cannot: nothing here deploys a workload into a cluster. So for the
    purpose of every layout that can actually be built, a cluster provider is
    what the catalogue already calls it — a managed service the requester asked
    for — and it is placed like one.

    That is what guarantees there is always somewhere for everything to go. The
    cluster options are still enumerated beside these, refused and with the
    reason, so the requester learns why Kubernetes is not on offer; they are
    just no longer the ONLY answer, which left "OKE and a Node.js app" with two
    refusals and nothing to choose.

    PUBLIC, AND SHARED WITH THE VALIDATOR ON PURPOSE. `enumerate_options` decides
    what a layout contains and `placeable_components` decides what a PROPOSED
    layout is allowed to contain. Two copies of this rule would drift, and the
    failure would be silent in the worst direction: a proposal judged against a
    different set of components than the one the resolver placed.
    """
    return [replace(c, provides_cluster=False) if c.provides_cluster else c
            for c in components]


def placeable_components(components: Sequence[ComponentFacts]) -> list[ComponentFacts]:
    """Exactly the components any valid layout must place, once each.

    A machine is excluded because it IS the host: "VM with OS" is what the others
    land on, not a workload competing for room on someone else's.
    """
    return _workloads(resolved_facts(components))


def _managed_option(components: Sequence[ComponentFacts]) -> Option | None:
    """Everything the cloud can run, run by the cloud; the rest on one machine.

    Returns None when nothing in the selection is available as a managed
    service. Offering an option identical to the consolidated one, under a name
    promising less operational burden, would be a lie told twice.
    """
    workloads = _workloads(components)
    managed = [c for c in workloads if c.supports(HOST_MANAGED)]
    if not managed:
        return None

    remaining = [c for c in workloads if not c.supports(HOST_MANAGED)]
    hosts = [Host(id=f"managed-{c.code}", host_mode=HOST_MANAGED,
                  components=(c.code,)) for c in managed]
    if remaining:
        hosts.append(Host(id="host-1", host_mode=HOST_VM,
                          components=tuple(c.code for c in remaining)))

    names = ", ".join(c.code for c in managed)
    return Option(
        key=MANAGED_OPTION,
        title=OPTION_TITLES[MANAGED_OPTION],
        summary=(f"The cloud runs {names}. Patching, backups and failover for "
                 f"it stop being yours."),
        hosts=tuple(hosts),
    )


# NOTHING IN THIS SYSTEM DEPLOYS A WORKLOAD INTO A CLUSTER, and the reason is a
# network route rather than missing code.
#
# The OKE blueprint builds a cluster whose Kubernetes API endpoint is private
# (`is_public_ip_enabled = false`) in a VCN where every subnet is private — its
# own bastion cannot take a public IP, and the module's output says to fetch the
# kubeconfig from the bastion by hand. The orchestrator runs in a container on an
# office machine with no ssh, no kubectl and no route into that VCN. Terraform's
# kubernetes provider has to reach the API server at plan time, so no module in
# this repository can deploy into one from here.
#
# For a cluster the SAME request builds there is a second, independent blocker:
# Terraform configures a provider before it creates resources, so a kubernetes
# provider cannot be pointed at a cluster the same apply is in the middle of
# creating. That needs two stages with separate state, whatever the network does.
#
# SO THE CLUSTER OPTIONS ARE REFUSED, NOT HIDDEN. They were offered, sized,
# priced and shown to approvers, and P.13's unit mechanism would have built a
# MACHINE from the workload's own VM blueprint and no cluster at all — a
# requester asking for Kubernetes receiving one VM, reported as success. That is
# the failure this phase exists to end, so the option now says what is true.
#
# A requester who wants a cluster still gets one: placement is optional (P.11),
# and an OKE request without a placement provisions the cluster exactly as every
# one has to date. What is withdrawn is the claim that the portal will also put
# the workloads on it.
CLUSTER_DEPLOYMENT_UNAVAILABLE = (
    "The portal can provision a cluster, but it cannot deploy workloads into "
    "one: the Kubernetes API endpoint is private and the orchestrator has no "
    "route to it. Ask for the cluster on its own and deploy {names} into it "
    "yourself, or choose a layout that builds machines.")


def cluster_deployment_offered() -> bool:
    """Whether a layout that puts workloads ON a cluster may be chosen (U.2).

    OFF means refused with the sentence above, which is what this portal did
    from the day the gap was found until 2026-09-11. ON means the layout
    resolves, prices and can be recorded.

    WHAT ON DOES NOT MEAN. It does not mean the workloads get deployed. The
    orchestrator still refuses a container host, and deliberately: removing that
    guard does not make the cluster path work, it makes a container host resolve
    to `oci-service-vm` and build a lone machine with NO CLUSTER while reporting
    success. That happened once already. So with this on, a request that chooses
    Kubernetes passes approval and then fails at provisioning, loudly, naming
    the missing network route.

    That is a worse outcome than a refusal at the point of choosing, and it was
    chosen knowingly: asked on 2026-09-11 whether to offer the route, grey it
    out, or build it as though the route existed, the platform owner chose the
    third with the consequence stated. This switch is how that is reversed
    without another code change -- and how it becomes truthful rather than
    optimistic on the day the route is opened.
    """
    return (os.getenv("CLUSTER_DEPLOYMENT_ENABLED", "true") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _no_deployment_path(workloads: Sequence[ComponentFacts]) -> str:
    names = ", ".join(c.code for c in workloads) or "your workloads"
    return CLUSTER_DEPLOYMENT_UNAVAILABLE.format(names=names)


def _cloud_only(workloads: Sequence[ComponentFacts]) -> list[ComponentFacts]:
    """Components the cloud runs and nobody can install on a machine.

    A component whose ONLY host mode is `managed` has no software to put on a
    host — an object store, a serverless function, a cloud database. There is no
    shape for it because there is no machine.
    """
    return [c for c in workloads if c.host_modes == frozenset({HOST_MANAGED})]


def _on_a_machine_refusal(cloud_only: Sequence[ComponentFacts]) -> str:
    """Why a machine-shaped layout cannot hold these.

    NOT "it could not be priced", which is what the requester saw before the
    catalogue knew these were cloud services: sizing correctly found no
    requirement row and the option came back unpriced, so a layout that CANNOT
    EXIST was reported in the words used for a layout whose numbers are merely
    missing. The two need different sentences, because one is answered by adding
    data and the other by choosing something else.
    """
    names = ", ".join(c.code for c in cloud_only)
    is_are = "is" if len(cloud_only) == 1 else "are"
    return (f"{names} {is_are} run by the cloud, so {'it' if len(cloud_only) == 1 else 'they'} "
            f"cannot be installed on a machine. 'Managed where available' places "
            f"{'it' if len(cloud_only) == 1 else 'them'} correctly and puts the rest "
            f"on their own host.")


def _consolidated_option(components: Sequence[ComponentFacts]) -> Option | None:
    """One machine carrying everything that needs one."""
    workloads = _workloads(components)
    if len(workloads) < 2:
        return None  # nothing to consolidate; it would equal `separated`
    # REFUSED, not quietly emptied. Dropping the cloud services and consolidating
    # the rest would build an environment missing components the requester asked
    # for, and the option would look like it had worked.
    cloud_only = _cloud_only(workloads)
    return Option(
        key=CONSOLIDATED_OPTION,
        title=OPTION_TITLES[CONSOLIDATED_OPTION],
        summary=("One machine cannot host a cloud service."
                 if cloud_only else
                 f"One machine hosts all {len(workloads)} components. Their "
                 f"resource needs are summed, not maximised."),
        hosts=(Host(id="host-1", host_mode=HOST_VM,
                    components=tuple(c.code for c in workloads)),),
        eligible=not cloud_only,
        reasons=(_on_a_machine_refusal(cloud_only),) if cloud_only else (),
    )


def _separated_option(components: Sequence[ComponentFacts]) -> Option | None:
    """A machine each."""
    workloads = _workloads(components)
    if not workloads:
        return None
    hosts = tuple(
        Host(id=f"host-{i}", host_mode=HOST_VM, components=(c.code,))
        for i, c in enumerate(workloads, start=1)
    )
    cloud_only = _cloud_only(workloads)
    return Option(
        key=SEPARATED_OPTION,
        title=OPTION_TITLES[SEPARATED_OPTION],
        summary=("A machine each, but a cloud service has no machine."
                 if cloud_only else
                 f"{len(hosts)} machines, one per component. The most isolated "
                 f"and the most expensive."),
        hosts=hosts,
        eligible=not cloud_only,
        reasons=(_on_a_machine_refusal(cloud_only),) if cloud_only else (),
    )


# Workloads that keep data. On a cluster their storage, failover and backups
# become the requester's burden rather than the platform's, which is worth
# saying out loud without refusing the choice.
STATEFUL = frozenset({"postgres16", "mysql", "mssql", "oracle-free", "mongodb",
                      "valkey", "opensearch"})


def _cluster_warnings(workloads: Sequence[ComponentFacts]) -> tuple[str, ...]:
    """Advice for putting data-keeping workloads on Kubernetes.

    Never a refusal. Running a database on a cluster is a legitimate choice, and
    the portal's job is to see it made knowingly rather than by default. Where
    the same component exists as a managed service that is named, because the
    alternative is more useful to a requester than the caution.
    """
    warnings = []
    for component in workloads:
        if component.code not in STATEFUL:
            continue
        message = (
            f"{component.code} keeps data. On Kubernetes its storage, failover "
            f"and backups become your team's responsibility rather than the "
            f"platform's.")
        if component.supports(HOST_MANAGED):
            message += (
                f" The cloud's managed {component.code} carries none of that — "
                f"it is offered as 'Managed where available'.")
        warnings.append(message)
    return tuple(warnings)


def _cluster_hosts(workloads: Sequence[ComponentFacts]) -> tuple[Host, ...]:
    """One container host per workload, not one host carrying all of them.

    A POD IS NOT A SHARED MACHINE, and modelling it as one had a real cost. OPA
    refuses two databases on a host — page cache, disk queue, and one patch
    taking both down — and that rule is right about a machine. On Kubernetes the
    two run in separate pods with separate limits, and the rule was firing
    against a shape that does not exist.

    A requester asking for OKE with Oracle and PostgreSQL was told "oracle-free
    and postgres16 may not share a host", which was true of the topology the
    portal had drawn and not of the one they had asked for.

    Sizing and price are unchanged in substance: the same components, the same
    per-component shapes, summed the same way. What changes is that each is
    asked for on its own, which is what the cluster would actually do.
    """
    return tuple(Host(id=f"cluster-{c.code}", host_mode=HOST_CONTAINER,
                      components=(c.code,))
                 for c in workloads)


def _existing_cluster_option(components: Sequence[ComponentFacts],
                             clusters: Sequence | None) -> Option | None:
    """Land the workloads on a cluster that already exists — which nothing can do.

    REFUSED BEFORE ENTITLEMENT IS CONSULTED, and the order is the point. Whether
    this requester may use a particular cluster does not matter while nothing can
    deploy to any cluster at all; answering the narrower question first would put
    two refusals on the screen and bury the one that decides it.

    Still returned rather than hidden, and still carrying whatever `api.clusters`
    found, so a requester sees both what they would be entitled to and why it
    cannot be used. An option that vanishes is indistinguishable from a portal
    that is broken.

    WHAT THIS DOES NOT MEAN. The entitlement, capacity and quota logic in
    `api.clusters` is not wrong and is not removed — it is DORMANT. P.5a built it
    a caller; this takes the caller away again for a reason that has nothing to do
    with entitlement, and `test_the_entitlement_machinery_is_intact_and_dormant`
    exists so it is not mistaken for dead code and deleted.
    """
    workloads = _workloads(components)
    if not workloads:
        return None

    found = tuple(clusters or ())
    hosts = _cluster_hosts(workloads)

    if not cluster_deployment_offered():
        return Option(
            key=EXISTING_CLUSTER_OPTION,
            title=OPTION_TITLES[EXISTING_CLUSTER_OPTION],
            summary="The portal cannot deploy into a cluster.",
            hosts=hosts,
            eligible=False,
            reasons=(_no_deployment_path(workloads),),
            # Whatever discovery found, if it found anything. None means nobody
            # looked, which is still not the same as "you have none" — but
            # neither answer changes this option's availability now.
            clusters=found,
            warnings=_cluster_warnings(workloads),
        )

    # ENTITLEMENT DECIDES ONCE DEPLOYMENT NO LONGER DOES (U.2). While nothing
    # could deploy to any cluster at all, asking whether THIS requester may use
    # a PARTICULAR one was a narrower question with no bearing on the answer, so
    # it was skipped and `api.clusters` went dormant. Now that the layout can be
    # chosen, the narrower question is the only one left -- and offering a
    # cluster the requester is not entitled to would be a worse failure than the
    # one it replaces, because it would be found at the cluster rather than at
    # the form.
    # THERE HAS TO BE A CLUSTER TO DEPLOY ONTO.
    #
    # This fell through to `eligible` when discovery found NOTHING, so "Deploy
    # onto an existing cluster" was offered — summarised as "on a cluster you
    # already have" — to a requester who has none. Selecting it passed the
    # workspace and failed at submit with "requires naming one", which is the
    # offering-what-cannot-be-done fault this phase exists to end, reintroduced
    # by the change that was meant to end it. Found on 2026-09-11 by checking a
    # real request before describing what it would show.
    #
    # `None` and `()` are different answers and get different sentences: nobody
    # looked, versus looked and found none. Neither is a cluster.
    if not found:
        return Option(
            key=EXISTING_CLUSTER_OPTION,
            title=OPTION_TITLES[EXISTING_CLUSTER_OPTION],
            summary="There is no existing cluster to deploy onto.",
            hosts=hosts,
            eligible=False,
            reasons=((
                "No Kubernetes cluster was found for this request, so there is "
                "nothing to deploy onto. Choose 'Provision a new cluster' to "
                "have one built as part of this request."
            ) if clusters is not None else (
                "The portal could not list existing clusters, so it cannot say "
                "which one this would land on. Choose 'Provision a new cluster' "
                "to have one built as part of this request."
            ),),
            warnings=_cluster_warnings(workloads),
        )

    usable = [c for c in found if getattr(c, "eligible", False)]
    if not usable:
        refusals = tuple(dict.fromkeys(
            reason for c in found for reason in getattr(c, "reasons", ())))
        return Option(
            key=EXISTING_CLUSTER_OPTION,
            title=OPTION_TITLES[EXISTING_CLUSTER_OPTION],
            summary="No cluster this request may use.",
            hosts=hosts,
            eligible=False,
            # Their words, not a summary of them: each says what is wrong with
            # that particular cluster, and a requester chasing entitlement needs
            # to know which one to chase.
            reasons=refusals or ("No cluster this request may use was found.",),
            clusters=found,
            warnings=_cluster_warnings(workloads),
        )

    return Option(
        key=EXISTING_CLUSTER_OPTION,
        title=OPTION_TITLES[EXISTING_CLUSTER_OPTION],
        summary="Workloads run as containers on a cluster you already have.",
        hosts=hosts,
        clusters=found,
        warnings=_cluster_warnings(workloads),
    )


def _new_cluster_option(components: Sequence[ComponentFacts]) -> Option | None:
    """Provision a cluster as part of this request.

    Always offered when a cluster was asked for, including when the requester
    has no existing cluster — it is the answer in that case, not a fallback.
    """
    providers = [c for c in components if c.provides_cluster]
    if not providers:
        return None

    workloads = _workloads(components)
    name = ", ".join(c.code for c in providers)

    # A cluster with nothing to deploy onto it is a cluster this portal CAN
    # build, so that case stays available and says so. It is the workloads that
    # have no path, not the cluster.
    if not workloads:
        return Option(
            key=NEW_CLUSTER_OPTION,
            title=OPTION_TITLES[NEW_CLUSTER_OPTION],
            summary=(f"A new {name} cluster on its own. Nothing is deployed "
                     f"into it — that is yours to do."),
            hosts=(Host(id="new-cluster", host_mode=HOST_MANAGED,
                        components=tuple(c.code for c in providers)),),
        )

    if not cluster_deployment_offered():
        return Option(
            key=NEW_CLUSTER_OPTION,
            title=OPTION_TITLES[NEW_CLUSTER_OPTION],
            summary="The portal cannot deploy into a cluster it builds.",
            hosts=_cluster_hosts(workloads),
            eligible=False,
            reasons=(_no_deployment_path(workloads),),
            warnings=_cluster_warnings(workloads),
        )

    # THE CLUSTER AND THE WORKLOADS ON IT ARE TWO HOSTS, not one. The cluster is
    # a managed service the cloud runs; the workloads are containers that land on
    # it. Collapsing them into a single container host would price the control
    # plane as though it were a workload, and lose the cluster itself from the
    # topology the requester is shown.
    return Option(
        key=NEW_CLUSTER_OPTION,
        title=OPTION_TITLES[NEW_CLUSTER_OPTION],
        summary=(f"A new {name} cluster, with your workloads running on it as "
                 f"containers."),
        hosts=(Host(id="new-cluster", host_mode=HOST_MANAGED,
                    components=tuple(c.code for c in providers)),
               *_cluster_hosts(workloads)),
        warnings=_cluster_warnings(workloads),
    )


def enumerate_options(components: Sequence[ComponentFacts],
                      clusters: Sequence | None = None) -> list[Option]:
    """Every layout worth offering, in the order the requester should see them.

    Which scenario applies is decided by the SELECTION, not by a flag. Asking for
    OKE or AKS is asking for a cluster, so the cluster options are the answer and
    "one machine or three" is not a question about it. Everything else gets
    Scenario A.

    The managed option appears in both, because a stateful workload put on
    Kubernetes should always be able to see the alternative that removes the
    operational burden it is about to take on.

    No judgement here — an option that policy forbids is still enumerated, so
    that `resolve_options` can return it with the reason attached rather than
    leaving the requester to guess why a choice they expected is missing.
    """
    # A CLUSTER ONLY CHANGES THE QUESTION IF SOMETHING CAN ACTUALLY RUN ON IT.
    #
    # The cluster options place every workload on a `container` host, and until
    # now they did that without asking whether the component runs as one. Apache
    # and SQL Server are `vm` in this catalogue — they have no container image the
    # portal builds — so "OKE and Apache" offered two layouts that put Apache on
    # Kubernetes, both of which then reported "cannot be sized: no requirement is
    # recorded for apache as container". That is the resolver proposing a layout
    # the catalogue already says is impossible, and then blaming the sizing table
    # for not having a number for it.
    #
    # Worse, it left the requester with nothing: the cluster branch returns ONLY
    # cluster options, so Apache — which does need a machine — had nowhere to go.
    #
    # So when no workload can be containerised, the cluster provider is not
    # hosting anything. It is an ordinary managed service the requester asked
    # for, which is exactly what the catalogue calls it, and the normal machine
    # layouts apply to everything else. "OKE and SQL Server" then resolves the way
    # it should: the cloud runs the cluster, the database gets a machine, and the
    # option is priced rather than refused.
    # THE CLUSTER OPTIONS ARE SHOWN, AND THEY ARE NOT THE ONLY ANSWER.
    #
    # They are refused — nothing deploys into a cluster from here — so making
    # them the whole reply left a Kubernetes request with two refusals and
    # nothing to choose. That is the screen this was reported for. They are
    # enumerated FIRST because the requester asked for a cluster and the reason
    # they cannot have workloads on it belongs at the top; then the layouts that
    # actually build follow underneath.
    #
    # Only offered when something could have gone on the cluster. Apache and SQL
    # Server are `vm` in this catalogue, so "OKE and SQL Server" is not a
    # thwarted Kubernetes deployment — it is a cluster and a database, and there
    # is nothing to explain.
    cluster_options: list[Option] = []
    if any(c.provides_cluster for c in components) and any(
            c.supports(HOST_CONTAINER) for c in _workloads(components)):
        cluster_options = [option for option in
                           (_existing_cluster_option(components, clusters),
                            _new_cluster_option(components))
                           if option is not None]

    # A cluster provider is placed as the managed service it is — see
    # `resolved_facts`, which the proposal validator shares so the two cannot
    # disagree about what a layout is supposed to contain.
    components = resolved_facts(components)

    builders = {
        MANAGED_OPTION: _managed_option,
        CONSOLIDATED_OPTION: _consolidated_option,
        SEPARATED_OPTION: _separated_option,
    }
    built = [builders[key](components) for key in OPTION_ORDER]
    return cluster_options + [option for option in built if option is not None]


def resolve_options(
    components: Iterable[ComponentFacts],
    environment: str,
    deployment_target: str,
    evaluate: Callable[[dict], dict],
    clusters: Sequence | None = None,
) -> list[Option]:
    """The options for this selection, each marked eligible or refused.

    `evaluate` is given a topology document and returns OPA's answer —
    `{"allow": bool, "violations": [str, ...]}`. Injected rather than imported
    so this stays testable without a running OPA, and so the caller decides what
    a policy outage means; here it can only mean "refused", never "allowed".
    """
    components = list(components)
    resolved: list[Option] = []

    for option in enumerate_options(components, clusters):
        # An option already refused when it was built — no entitled cluster, say
        # — is not sent to the policy: it has its reason, and asking whether a
        # layout nobody can have is permitted would overwrite that reason with a
        # less useful one.
        if not option.eligible:
            resolved.append(option)
            continue

        answer = evaluate(topology_document(option, environment, deployment_target))
        allowed = bool(answer.get("allow"))
        violations = tuple(answer.get("violations", ()))
        if allowed:
            resolved.append(option)
            continue
        # A refusal with no sentence is the failure mode this module exists to
        # prevent, so supply one rather than render an empty "unavailable".
        reasons = violations or (
            "This layout is not permitted here, and the policy gave no reason. "
            "Report this: a refusal without an explanation is a defect.",)
        resolved.append(Option(
            key=option.key, title=option.title, summary=option.summary,
            hosts=option.hosts, eligible=False, reasons=reasons,
            clusters=option.clusters, warnings=option.warnings,
        ))
    return resolved
