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

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.session import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now (replaces the deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc)


class Project(Base):
    """A project that can own infrastructure requests (a lookup)."""

    __tablename__ = "project"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


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

    sizing_anchors: Mapped[list["SizingAnchor"]] = relationship(
        back_populates="technology"
    )


class Environment(Base):
    """An existing environment, used for lookups (e.g. add/resize targets)."""

    __tablename__ = "environment"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # prod | non-prod  — drives HA/backup/monitoring defaults (F-CAT-08)
    environment_class: Mapped[str] = mapped_column(String(16), default="non-prod")
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
    # Environment tier/stage for 'create' (dev|test|sit|uat|preprod|prod|dr) — 6.2.
    environment_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Advanced options (6.5): a flexible JSON bag (region, HA, backup retention,
    # monitoring level, support tier, encryption, …). A few drive cost; the rest
    # are captured for the approver. Nullable so existing rows are unaffected.
    advanced_options: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)
    # For 'decommission': the reference of the previously provisioned request
    # whose resources this request tears down (2.9).
    source_reference: Mapped[str | None] = mapped_column(String(20), nullable=True)
    data_classification: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Governance metadata captured on the form (increment 6.1, from the UX
    # brief). Nullable so existing rows and drafts are unaffected; required-on-
    # submit rules live in api/validation.py, not the database.
    business_justification: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(16), nullable=True)  # low|medium|high|critical
    business_criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)  # tier1..tier4
    required_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    application_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    business_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    technical_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    environment_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)

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
    """

    __tablename__ = "request_component"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("request.id"))
    technology_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    size: Mapped[str | None] = mapped_column(String(16), nullable=True)

    request: Mapped["Request"] = relationship(back_populates="components")


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
