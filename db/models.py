"""Reference-data tables for the Infra Portal (increment 1.1).

Scope is deliberately limited to the *reference data* the portal reads:
projects, cost centres, technologies, existing environments, sizing anchors,
and rate cards (ARCHITECTURE.md §5). The transactional tables (request,
estimate, approval, registry, audit_log) are added in the later increments
that first use them.

Column types are kept portable (no Postgres-only types) so the same models run
against an in-memory database in the tests.
"""

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.session import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now (replaces the deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc)


class Project(Base):
    """A project that can own infrastructure requests.

    More than a lookup: a project carries an owner to chase, a lifetime, and the
    identity of whoever asked for it. Environments and quotas hang off it, so a
    project nobody owns is a set of resources nobody is accountable for.
    """

    __tablename__ = "project"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    # Disabling is a deliberate act, so it hides the project from the form AND
    # refuses a submission naming it. Expiry, below, is softer: it arrives by the
    # calendar rather than by decision.
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    description: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # Who to chase when this expires. A project with no owner is nobody's problem.
    owner_email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Captured from the signed-in identity, never typed, so it cannot be wrong.
    requested_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    requested_by_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Null means no expiry, which is right for most projects. A date here does
    # NOT touch anything already provisioned — a record expiring must never tear
    # down infrastructure.
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Prefills the request form and heads off the commonest data-quality error:
    # right project, wrong cost centre.
    cost_centre_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # Which expiry stage has already been announced ('expiring' | 'expired'), so
    # the sweep says it once rather than every cycle. Cleared if the date is
    # pushed out, so a renewed project can warn again later.
    expiry_notified: Mapped[str | None] = mapped_column(String(16), nullable=True)


class CostCentre(Base):
    """A cost centre money is charged against (a lookup)."""

    __tablename__ = "cost_centre"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Subsidiary(Base):
    """A subsidiary a request can be raised for (a lookup)."""

    __tablename__ = "subsidiary"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Technology(Base):
    """A technology that can be requested, with its lifecycle state (F-CAT-02)."""

    __tablename__ = "technology"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    # certified | preview | deprecated | eol  (deprecated warns, eol blocks)
    lifecycle_state: Mapped[str] = mapped_column(String(16), default="certified")
    # The cloud resource a request for this technology provisions: oci-bucket
    # (object storage, the default) or oci-instance (a stoppable compute VM).
    # Drives the orchestrator's provisioning branch and whether the control plane
    # offers stop/start.
    resource_kind: Mapped[str] = mapped_column(String(16), default="oci-bucket")
    # Which deployment targets this technology is available on (CSV of target
    # codes). Default = all: generic software runs anywhere. A cloud-managed
    # service (e.g. Amazon RDS) sets just its own cloud. Drives the target-aware
    # catalogue filter + submit validation (F-CAT).
    targets: Mapped[str] = mapped_column(String(80), default="onprem,azure,oci,aws,gcp")

    sizing_anchors: Mapped[list["SizingAnchor"]] = relationship(
        back_populates="technology"
    )


# The environment tiers this estate runs, in lifecycle order. A CONSTRAINED
# vocabulary rather than free text: with a per-tier network map, a typo would be
# a tier with no VCN behind it. That is refused rather than misrouted — the safe
# failure — but it is better not to be reachable at all.
#
# DR was proposed and deliberately deferred on 16 Aug 2026. Adding it is not a
# one-line change: disaster recovery generally implies a different REGION, and
# the network map would need a region alongside its subnets. All six below are
# in me-dubai-1.
ENVIRONMENT_TIERS: tuple[str, ...] = (
    "Development",
    "QMG",
    "Pre-Test",
    "Test",
    "UAT",
    "Production",
)

# What the two values that preceded this vocabulary become. Kept in code rather
# than done once by hand, so a database seeded from an older dump converges
# instead of carrying values nothing recognises.
LEGACY_ENVIRONMENT_CLASSES: dict[str, str] = {
    "prod": "Production",
    "non-prod": "UAT",
}


class Environment(Base):
    """An existing environment, used for lookups (e.g. add/resize targets)."""

    __tablename__ = "environment"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # One of ENVIRONMENT_TIERS. Drives HA/backup/monitoring defaults (F-CAT-08),
    # and from 16 Aug 2026 it is also the key for the per-tier network map: one
    # VCN per tier, so a cluster or a machine lands in the network belonging to
    # its own tier and no other.
    #
    # It used to hold only prod | non-prod, which could not express the estate:
    # development, QMG, pre-test, test and UAT were one value between them. A
    # per-tier network map keyed on that would have put UAT and development in
    # the same VCN while appearing to honour "one VCN per tier".
    environment_class: Mapped[str] = mapped_column(String(16), default="Development")
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))


class SizingAnchor(Base):
    """Effective-dated CPU/RAM/storage anchor per technology + size (F-CAT-07)."""

    __tablename__ = "sizing_anchor"

    id: Mapped[int] = mapped_column(primary_key=True)
    technology_id: Mapped[int] = mapped_column(ForeignKey("technology.id"))
    size: Mapped[str] = mapped_column(String(16))  # small | medium | large
    vcpu: Mapped[int] = mapped_column()
    memory_gb: Mapped[int] = mapped_column()
    storage_gb: Mapped[int] = mapped_column()
    # Effective-dated + versioned so anchors can change over time with a diff.
    effective_from: Mapped[date] = mapped_column(Date, default=date(2026, 1, 1))
    version: Mapped[int] = mapped_column(default=1)

    technology: Mapped["Technology"] = relationship(back_populates="sizing_anchors")


class RateCard(Base):
    """A priced rate: on-prem, licence, or cached cloud price (F-FIN-04).

    One flexible table covers ARCHITECTURE.md §5's onprem_rate, licence_rate and
    cloud_price_cache (Azure/OCI). `discount_pct` makes it discount-aware.
    """

    __tablename__ = "rate_card"

    id: Mapped[int] = mapped_column(primary_key=True)
    # onprem | licence | cloud_azure | cloud_oci
    kind: Mapped[str] = mapped_column(String(16))
    item: Mapped[str] = mapped_column(String(64))  # e.g. "vcpu-hour", "postgres-licence"
    unit: Mapped[str] = mapped_column(String(32))  # e.g. "per vCPU/hour", "per month"
    rate: Mapped[float] = mapped_column(Numeric(12, 4))
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    discount_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    effective_from: Mapped[date] = mapped_column(Date, default=date(2026, 1, 1))


class Budget(Base):
    """A monthly spend ceiling for a cost centre (F-FIN-02, E3.3).

    Optional per cost centre — a cost centre with no row here is ungated. Set by
    an admin (or loaded from finance); the submit-time guardrail compares the
    projected committed spend for the cost centre against monthly_limit.
    """

    __tablename__ = "budget"

    id: Mapped[int] = mapped_column(primary_key=True)
    cost_centre_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    monthly_limit: Mapped[float] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ActualCost(Base):
    """The actual billed monthly cost for a provisioned request (F-FIN-01, E3.5).

    The 'actual' side of actual-vs-estimate variance. Recorded manually / loaded
    now; a live cloud-billing sync fills the same slot later. One current row per
    request (upserted); `alerted` tracks whether a drift alert already fired for
    the current figure, so re-recording the same value doesn't re-alert.
    """

    __tablename__ = "actual_cost"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    billed_monthly: Mapped[float] = mapped_column(Numeric(14, 2))
    period: Mapped[str | None] = mapped_column(String(16), nullable=True)  # e.g. "2026-07"
    source: Mapped[str] = mapped_column(String(32), default="manual")
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Quota(Base):
    """A cap on the number of environments a project may have (F-FIN-08, E3.9).

    Optional per project — a project with no row here is ungated. The submit-time
    guardrail (create requests only) compares the project's active environment
    count against max_environments.
    """

    __tablename__ = "quota"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    max_environments: Mapped[int] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ShutdownPolicy(Base):
    """The single global auto-shutdown schedule (F-FIN-06), editable by an admin
    from the portal. Exactly one row (id=1). When absent, the .env defaults apply,
    so behaviour is unchanged until an admin edits it. Per-request overrides
    (increment B) take precedence over this."""

    __tablename__ = "shutdown_policy"

    id: Mapped[int] = mapped_column(primary_key=True)  # always 1 (a singleton)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    days: Mapped[str] = mapped_column(String(32), default="mon-fri")
    start_hm: Mapped[str] = mapped_column(String(5), default="08:00")  # "HH:MM"
    end_hm: Mapped[str] = mapped_column(String(5), default="20:00")
    tz: Mapped[str] = mapped_column(String(64), default="UTC")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Request(Base):
    """A provisioning request (increment 1.3): the first transactional table.

    Drafts may be incomplete (fields nullable). Submission runs authoritative
    server-side validation before status moves from 'draft' to 'submitted'.
    Data classification is captured here (F-SEC-02). Sizing, cost, policy and
    Jira are added by later increments.
    """

    __tablename__ = "request"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | submitted
    # A short human-readable reason for the current status (e.g. why an apply
    # failed), surfaced in the portal. Null unless there's something to explain.
    status_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Consecutive failed handoffs to the orchestrator. The poller retries every
    # cycle, which is right for a transient error and wrong for a permanent one:
    # a request needing a setting nobody has set retried forever, several times a
    # minute, for as long as it existed. Reset on success and on a manual retry.
    provision_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    requester: Mapped[str] = mapped_column(String(120))
    # The requester's display name, captured from the sign-in identity at
    # creation (the requester column holds the email/username).
    requester_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # The subsidiary the request is raised for (chosen on the form).
    subsidiary: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # create | add | resize | decommission
    request_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    project_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_centre_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Where it runs: onprem | azure | oci (drives pricing).
    deployment_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # New environment name (for 'create'); target existing environment (others).
    environment_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_environment: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Environment tier/stage for 'create' — one of ENVIRONMENT_TIERS (6.2).
    # THE key for the per-tier network map: under one VCN per tier this
    # decides which network the request's infrastructure is built in, so a
    # value outside the vocabulary is a request that cannot be placed.
    environment_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Advanced options (6.5): a flexible JSON bag (region, HA, backup retention,
    # monitoring level, support tier, encryption, …). A few drive cost; the rest
    # are captured for the approver. Nullable so existing rows are unaffected.
    advanced_options: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)
    # For 'decommission': the reference of the previously provisioned request
    # whose resources this request tears down (2.9). For 'refresh': the TARGET
    # environment being refreshed (the one whose data is replaced).
    source_reference: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # For 'refresh' (F-LCM-03): the higher environment whose data is copied down
    # into the target (source_reference). Null for other request types.
    refresh_from_reference: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # For 'restore' (F-LCM-06): the Backup id to restore the target
    # (source_reference) to. Null for other request types.
    restore_backup_id: Mapped[int | None] = mapped_column(nullable=True)
    # For 'dns' (GAP-ANALYSIS step 5): the record to create for the target
    # environment (source_reference). `dns_value` is what it points at — left
    # blank, the orchestrator uses the environment's own recorded address, so the
    # record follows the thing it names. Null for other request types.
    dns_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    dns_type: Mapped[str | None] = mapped_column(String(8), nullable=True)  # A|CNAME
    dns_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data_classification: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Governance metadata captured on the form (increment 6.1, from the UX
    # brief). Nullable so existing rows and drafts are unaffected; required-on-
    # submit rules live in api/validation.py, not the database.
    business_justification: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(16), nullable=True)  # low|medium|high|critical
    business_criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)  # tier1..tier4
    required_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Chosen expiry for a 'temporary' environment (F-CAT). Drives its TTL. Null
    # for other request types (sandbox uses a short policy default instead).
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    application_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    business_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    technical_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    environment_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Group-based ownership (F-IAM-09): the directory/Jira group that owns this
    # environment. Durable — it survives an individual owner leaving, and its
    # members can manage the environment. Null = individual ownership only.
    owner_group: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    # Approval SLA (F-GOV-01): when the request entered 'submitted' (the SLA
    # clock start), and when a breach was escalated (so we escalate only once).
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Four-eyes (F-GOV-08): when a self-approval block was notified (once).
    four_eyes_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Change window (F-GOV-05): when provisioning was first held outside the window.
    change_window_held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Approval quorum (F-GOV-06): when provisioning was first held awaiting the
    # required number of distinct Jira approvers (so we audit the hold once).
    quorum_held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # TTL notification state (F-FIN-07): null | "expiring" | "expired" — so the
    # poller warns/flags a non-prod environment's expiry once per TTL cycle;
    # cleared on renewal so a future approach re-warns.
    ttl_notified: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Ownership orphan flag (F-LCM-10): when the environment was first flagged as
    # orphaned (no resolvable owner / owner left), so it's flagged once; cleared
    # on an ownership transfer.
    orphaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Drift detection (F-LCM-09): the result + time of the last drift check
    # (terraform plan vs the applied state). Null until first checked.
    drift_detected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    drift_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cloud state sync (read-only reconciliation): does the portal's registry match
    # the actual cloud state? 'in-sync' | 'drifted' | 'unknown'. Null until first
    # reconciled. `state_synced_at` is the last reconciliation time.
    state_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    state_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Denormalised at submit from the components: the cloud resource this
    # environment provisions (oci-bucket | oci-instance). Lets the handoff and
    # orchestrator branch without re-deriving from the catalogue. Null = bucket.
    resource_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Per-request auto-shutdown override (F-FIN-06 increment B): a partial policy
    # {enabled?, days?, start?, end?, tz?} MERGED over the global policy (override
    # wins). Null = inherit the global schedule.
    shutdown_override: Mapped[dict | None] = mapped_column(JSON, default=None, nullable=True)
    # True while auto-shutdown itself has this environment powered down, so the
    # sweep only auto-starts what it stopped (never a manual stop). Cleared on any
    # start or manual stop.
    auto_stopped: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Policy waiver (F-GOV-02): a documented, expiring exception that lets a
    # request pass the OPA policy gate despite violations. Holds
    # {reason, granted_by, granted_at, expires_at}. Nullable — most requests have
    # none; only an authorised approver (not the requester) can grant one.
    waiver: Mapped[dict | None] = mapped_column(JSON, default=None, nullable=True)

    # An environment is made of one or more components, each a technology with
    # its own size (ARCHITECTURE.md §5 — the 'component' model, 1.3a).
    components: Mapped[list["RequestComponent"]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="RequestComponent.id",
    )
    # The server-computed cost estimate captured at submission (1.6).
    estimate: Mapped["Estimate | None"] = relationship(
        back_populates="request", uselist=False, cascade="all, delete-orphan"
    )
    # The Jira approval raised at submission (1.8).
    approval: Mapped["Approval | None"] = relationship(
        back_populates="request", uselist=False, cascade="all, delete-orphan"
    )


class RequestComponent(Base):
    """One technology + its size within a request (1.3a).

    Sizing (1.4) and cost (1.5) are resolved per component, then summed.

    The four columns below carry the requester's explicit choices from the
    component detail form. All are nullable: a request raised before the form
    existed, or a draft that has not reached the detail step, resolves from the
    size anchor exactly as it always did.
    """

    __tablename__ = "request_component"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("request.id"))
    technology_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    size: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The technology version to install, e.g. "1.24" for nginx. Only versions the
    # platform can actually deliver are offered — a version the machine ignores is
    # the Redis-6-sold-as-7 bug with a dropdown in front of it.
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The OS image OCID the requester picked, from the hourly cloud cache. Null
    # means "whatever the platform default is", which is how it worked before.
    image: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Explicit shape. When set these OVERRIDE the size anchor for sizing, pricing
    # and the built machine, so what the approver sees priced is what gets built.
    vcpu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)

    request: Mapped["Request"] = relationship(back_populates="components")


class ComponentOption(Base):
    """A value the portal is willing to offer for one component detail field.

    This table is the *authority* for the component detail form: the browser may
    render whatever it likes, but a submitted value is only accepted if it
    appears here (or in the sizing anchors, which supply the numeric options for
    free — see api/component_options.py). That keeps the "client holds no
    authority" rule intact for a form whose whole purpose is free choice.

    `deployment_target` empty means "on every target". `source` records who put
    the row here — "seed" for catalogue data, and later the hourly cloud fetch
    replaces only its own rows, leaving hand-curated ones alone.
    """

    __tablename__ = "component_option"

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_target: Mapped[str] = mapped_column(String(16), default="")
    technology_code: Mapped[str] = mapped_column(String(48))
    # version | vcpu | memory_gb | storage_gb
    field: Mapped[str] = mapped_column(String(16))
    # Held as text so one table covers versions and numbers; numeric fields are
    # cast on the way out. Sized for an OCID (~100 chars) rather than for a
    # version string: the fetched OS images are stored by OCID, and SQLite — what
    # the tests run on — ignores VARCHAR limits, so a too-short column here would
    # pass every test and fail only against the real Postgres.
    value: Mapped[str] = mapped_column(String(200))
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="seed")
    # Structured facts about the option that are not for display — a compute
    # shape's OCPU and memory limits, for instance. Held here rather than encoded
    # into `label` and parsed back out with a regex: a label is human-readable
    # text that someone will reasonably reword one day, and validation must not
    # break when they do.
    attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # When a fetched row was last confirmed to exist in the cloud. Null for
    # hand-curated rows, which have no refresh cycle. The console reads the
    # newest of these as "last refreshed", so a fetch that silently stopped
    # running shows up as a stale timestamp rather than as nothing at all.
    refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class Estimate(Base):
    """Server-computed cost estimate tied to a request (1.6, ARCHITECTURE.md §5).

    Captured at submission so the approved cost is a stored fact, not something
    recomputed differently later. `breakdown` keeps the full sizing + cost
    snapshot for auditability.
    """

    __tablename__ = "estimate"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("request.id"), unique=True)
    deployment_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    one_time: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    monthly: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    annual: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    request: Mapped["Request"] = relationship(back_populates="estimate")


class Approval(Base):
    """The Jira approval raised for a request (1.8, ARCHITECTURE.md §5).

    Approval authority lives in Jira (P1); this row records the ticket key and
    status. The ticket body carries config + cost + plan preview together.
    SLA timers / approver chains (F-GOV-01) come in a later increment.
    """

    __tablename__ = "approval"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("request.id"), unique=True)
    jira_key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected
    ticket_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ticket_body: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    request: Mapped["Request"] = relationship(back_populates="approval")


class ProvisionedResource(Base):
    """A resource actually created by an apply (2.6b; ARCHITECTURE.md §5 registry).

    Records what exists so it can be tracked, given a TTL, and destroyed.
    """

    __tablename__ = "provisioned_resource"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # e.g. oci-bucket
    name: Mapped[str] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ttl_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # active | decommissioned
    lifecycle_state: Mapped[str] = mapped_column(String(16), default="active")
    # running | stopped — operational power state, actuated from the portal
    # (cloud-sync increment 2). Kept separate from lifecycle_state so stopping a
    # resource doesn't drop it from the TTL / health / billing queries that filter
    # on active resources.
    power_state: Mapped[str] = mapped_column(String(16), default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReportSubscription(Base):
    """A scheduled report subscription (F-RPT-11). Generated on its cadence; each
    run is stored (viewable) and optionally POSTed (signed) to target_url."""

    __tablename__ = "report_subscription"

    id: Mapped[int] = mapped_column(primary_key=True)
    report: Mapped[str] = mapped_column(String(24))    # forecast | anomalies | estate
    cadence: Mapped[str] = mapped_column(String(12))   # daily | weekly | monthly
    target_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    secret: Mapped[str | None] = mapped_column(String(200), nullable=True)  # HMAC for target_url
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_due: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReportRun(Base):
    """A generated report snapshot for a subscription (F-RPT-11), viewable in the
    portal — always available regardless of external delivery."""

    __tablename__ = "report_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(index=True)
    report: Mapped[str] = mapped_column(String(24))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    delivered: Mapped[str | None] = mapped_column(String(16), nullable=True)  # None|delivered|failed
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class LeaderLease(Base):
    """A single-active leadership lease (F-OPS-01). One row per lease name; the
    holder renews expires_at each tick. If it dies, the lease expires and another
    replica takes over — so the poller runs in exactly one API replica at a time."""

    __tablename__ = "leader_lease"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str] = mapped_column(String(80))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RoleMapping(Base):
    """A Jira group -> portal role mapping (F-IAM-01). DB-backed so admins manage
    it from the portal instead of an env var — the one authorization decision the
    portal itself owns. Unblocks turning live RBAC enforcement on."""

    __tablename__ = "role_mapping"

    jira_group: Mapped[str] = mapped_column(String(120), primary_key=True)
    role: Mapped[str] = mapped_column(String(40))
    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Blueprint(Base):
    """A certified recipe for building one technology on one cloud (F-CAT-10).

    The recipe itself lives in git (a Terraform module, Helm chart or playbook)
    where it is reviewed and diffed. This row is the *pointer* plus the
    certification record — the deliberate decision that a given version is
    approved for building real infrastructure, and by whom.

    Keyed on (technology, target) so the same technology can be certified on one
    cloud and not another. A technology is 'automated' on a target precisely when
    a certified row exists here, so the catalogue's honesty badge and the
    execution layer can never drift apart.
    """

    __tablename__ = "blueprint"

    technology_code: Mapped[str] = mapped_column(String(48), primary_key=True)
    deployment_target: Mapped[str] = mapped_column(String(16), primary_key=True)
    # Path of the module in the orchestrator, e.g. 'oci/postgres'.
    blueprint_ref: Mapped[str] = mapped_column(String(160))
    # What this recipe builds, copied from the orchestrator's manifest at
    # certification. Recorded here so the provisioning path derives the resource
    # kind from the SAME source as the catalogue badge — when they were separate,
    # a certified Apache blueprint still provisioned an object-storage bucket.
    resource_kind: Mapped[str] = mapped_column(String(32), default="")
    version: Mapped[str] = mapped_column(String(32), default="")
    # certified = usable for real provisioning; draft = present but not approved.
    status: Mapped[str] = mapped_column(String(16), default="draft")
    certified_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(400), nullable=True)


class UserRole(Base):
    """A portal role granted to a named person (F-IAM-01).

    The portal's roles have no external source here: Entra provides login only
    (no group claims), and Jira's authority model is per-ticket Assignment
    Groups, which answer 'who may act on this ticket' rather than 'who may
    administer the portal'. So authorisation is maintained in the portal itself
    while identity stays federated.

    One row per (email, role) so a person can hold several. Anyone authenticated
    but unlisted falls back to the default role — deliberately NOT an admin.
    """

    __tablename__ = "user_role"

    email: Mapped[str] = mapped_column(String(160), primary_key=True)
    role: Mapped[str] = mapped_column(String(40), primary_key=True)
    granted_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Setting(Base):
    """A runtime override for a NON-SECRET governance/FinOps/AI setting (F-OPS-09).

    A row here overrides the matching environment variable at read time (see
    api/settings.py precedence: DB -> .env -> default), so a platform admin can
    tune the portal's policy posture from the Admin console instead of editing
    .env and redeploying. Only allow-listed, non-secret keys are ever stored;
    secrets and security/provisioning switches stay in .env/vault by design and
    are never written here. `value` is the raw string, parsed by the reader."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(String(400))
    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RateLimitCounter(Base):
    """A shared fixed-window rate-limit counter (F-OPS-01 / F-SEC-09). One row per
    (client, minute), incremented atomically so the per-minute limit is global
    across API replicas instead of per-process. Pruned periodically."""

    __tablename__ = "rate_limit_counter"

    key: Mapped[str] = mapped_column(String(180), primary_key=True)   # "<identity>:<window_epoch>"
    window_epoch: Mapped[int] = mapped_column(index=True)             # unix minute bucket
    count: Mapped[int] = mapped_column(default=0)


class WebhookSubscription(Base):
    """An admin-configured outbound webhook endpoint (F-INT-10). Lifecycle events
    are delivered here, HMAC-signed. `secret` is a shared HMAC key the admin sets
    (runtime config, never in git); the payload carries no portal secrets."""

    __tablename__ = "webhook_subscription"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(500))
    secret: Mapped[str] = mapped_column(String(200))
    events: Mapped[list] = mapped_column(JSON, default=list)  # [] or ["*"] = all events
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WebhookDelivery(Base):
    """One at-least-once delivery record for an outbound webhook (F-INT-10)."""

    __tablename__ = "webhook_delivery"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(index=True)
    event_id: Mapped[int] = mapped_column(index=True)  # AuditLog.id
    event: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|delivered|failed
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookState(Base):
    """Singleton cursor for the webhook fan-out (F-INT-10): the last audit id
    enqueued, so only NEW events fan out (history isn't backfilled)."""

    __tablename__ = "webhook_state"

    id: Mapped[int] = mapped_column(primary_key=True)  # always 1
    last_event_id: Mapped[int] = mapped_column(default=0)


class AccessGrant(Base):
    """A time-bound just-in-time access grant to a provisioned environment
    (F-IAM-07). The credential itself is delivered by the vault via a one-time link
    (F-INT-05) and is NEVER stored here — only the grant metadata + a vault handle,
    for expiry, revoke, and audit."""

    __tablename__ = "access_grant"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), index=True)  # the env's request ref
    grantee: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(32))   # ssh | db-read | db-admin | read-only | admin
    granted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|expired|revoked
    vault_handle: Mapped[str | None] = mapped_column(String(120), nullable=True)  # NOT the secret


class Backup(Base):
    """A restore-point for a provisioned environment (F-LCM-06). Owner-initiated
    (taking one needs no approval); restoring FROM one is approval-governed. Mock
    records metadata (no real snapshot); live is an orchestrator extension point.
    """

    __tablename__ = "backup"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), index=True)  # the env's request ref
    label: Mapped[str] = mapped_column(String(120))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    """Append-only, hash-chained audit trail (1.9; F-SEC-01 foundation).

    Every privileged step (approval, handoff, provisioning) is recorded. Each
    row's hash chains to the previous one, so tampering is detectable. Full
    tamper-evidence hardening (signing, verification tooling) comes in E1.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    reference: Mapped[str | None] = mapped_column(String(20), nullable=True)
    jira_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Distributed-tracing correlation id (F-OPS-04): groups every step of one
    # request across the stack. Metadata only — deliberately NOT part of the
    # tamper-evident hash, so the chain is unchanged.
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class ApiKey(Base):
    """A programmatic access credential (F-INT-01, E1).

    Only the SHA-256 hash of the key is stored — the plaintext key is shown once
    at creation and never again. The key authenticates as `identity`, so it
    inherits that user's roles and can never exceed them.
    """

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    identity: Mapped[str] = mapped_column(String(120), index=True)
    label: Mapped[str] = mapped_column(String(120), default="api key")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# How a technology reaches a requester, and therefore who operates it.
#
# "SaaS versus IaaS" is the question people ask; these are the answers that
# actually differ. Nothing in this catalogue is SaaS in the strict sense (a
# finished business application), so naming the models after what they DO avoids
# a label that would be wrong however it were used.
DELIVERY_MANAGED = "managed"        # the cloud runs it; we consume an endpoint
DELIVERY_SOFTWARE = "software"      # installed on a machine the customer owns
DELIVERY_MACHINE = "machine"        # a bare VM; nothing is installed on it
DELIVERY_CAPABILITY = "capability"  # an outcome, not an installable thing
DELIVERY_MODELS = (DELIVERY_MANAGED, DELIVERY_SOFTWARE, DELIVERY_MACHINE,
                   DELIVERY_CAPABILITY)


class TechnologyDelivery(Base):
    """How each catalogue entry is delivered — a fact about the entry, not its name.

    The agent used to infer this from the CODE: anything not prefixed `oci-`,
    `aws-`, `azure-` or `gcp-` was assumed to be software you install on a
    machine. That is a naming convention doing a domain model's job, and it was
    wrong in both directions. `postgres16` is OCI's MANAGED database service and
    reads as software by that rule; "Backup & Recovery" is an outcome nobody can
    install and reads as software too — REQ-2026-0183 spent a real machine
    discovering that `dnf install backup` finds nothing.

    A NEW TABLE rather than a column on Technology, for the reason
    CertificationProof gives: create_all adds missing tables and NOT missing
    columns, so a new column would silently not exist on a database that already
    has a technology table, and the agent would read None for every entry and
    fall back to exactly the guess this replaces.
    """

    __tablename__ = "technology_delivery"

    technology_code: Mapped[str] = mapped_column(String(48), primary_key=True)
    # managed | software | machine | capability
    delivery_model: Mapped[str] = mapped_column(String(16))
    # Why, in a sentence — shown to a requester who picked something the portal
    # cannot build, so a refusal explains rather than merely declines.
    note: Mapped[str] = mapped_column(String(300), default="")


class RecipeRefutation(Base):
    """A recipe a real machine disproved, so no machine has to disprove it twice.

    REQ-2026-0183 asked for "Backup & Recovery". The agent guessed `dnf install
    backup`, booted a real VM, and the machine said "backup NOT INSTALLED" —
    correct, honest, and five minutes and a machine to learn that a capability
    name is not an RPM. Six catalogue entries are in that category (backup,
    logging, monitoring, api-gateway, service-mesh, k8s), and NOTHING remembered
    the answer: the next request would spend another machine on the identical
    guess, and so would the one after that.

    KEYED ON THE RECIPE, NOT THE COMPONENT. keycloak was refuted as a package
    guess on REQ-2026-0177 and then SUCCEEDED as an archive install on 0178 — a
    component-level block would have prevented that. What was disproved is one
    specific way of installing something, and changing the recipe must be allowed
    to change the answer.

    A NEW TABLE rather than a column on CertificationProof, for the reason that
    table gives itself: create_all adds missing tables and not missing columns,
    so a new column would silently not exist on a database that already has this
    schema — which is how the certification sweep ran for a day against a table
    that was not there.

    Refutations EXPIRE on the same clock as certifications. A package absent from
    Oracle Linux today may be packaged tomorrow, and evidence about the world has
    a shelf life exactly as evidence about a recipe does.
    """

    __tablename__ = "recipe_refutation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    technology_code: Mapped[str] = mapped_column(String(48), index=True)
    deployment_target: Mapped[str] = mapped_column(String(16))
    # What was tried, as a stable digest of the install-relevant fields only.
    # Cosmetic differences (a reworded note) must not make an old refutation
    # look like a new recipe.
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # Which machine disproved it, so the refusal can cite its evidence.
    proof_reference: Mapped[str] = mapped_column(String(48), default="")
    # The machine's own words. A refusal that says "this was tried" without
    # saying what happened teaches nobody anything.
    detail: Mapped[str] = mapped_column(String(1000), default="")
    refuted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CertificationProof(Base):
    """One attempt to prove a blueprint still builds, by building it (C2).

    A NEW TABLE rather than columns on Blueprint, for two reasons. There is no
    migration mechanism here — `create_all` adds missing tables but not missing
    columns, so new columns would silently not exist on a database that already
    has a blueprint table. And a proof is an event with a history worth keeping,
    not a single mutable field: 'this failed three times then passed' is exactly
    the kind of thing someone will want to read later.

    Certification is DERIVED from these rows (ARCHITECTURE.md P8): a blueprint is
    certified while its most recent proof passed and is younger than the validity
    period. Nobody sets a flag.
    """

    __tablename__ = "certification_proof"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    technology_code: Mapped[str] = mapped_column(String(48), index=True)
    deployment_target: Mapped[str] = mapped_column(String(16))
    resource_kind: Mapped[str] = mapped_column(String(32), default="")
    # The proof's own reference, e.g. PROOF-OCI-OKE-20260821T0930. Carries the
    # marker that scopes teardown — see ARCHITECTURE.md §4, "only what it created".
    reference: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    # running | passed | failed | refused | abandoned
    #   refused  = never built. Over the cost cap, or the sandbox was not set.
    #   abandoned = built and could not be torn down. The loudest outcome there
    #               is, because it means the runner left something billing.
    status: Mapped[str] = mapped_column(String(16), default="running")
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # What the plan was priced at, and the ceiling it was checked against, so a
    # refusal can say "1,240 exceeds the 200 cap" rather than just "too expensive".
    planned_monthly: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    cost_cap: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
