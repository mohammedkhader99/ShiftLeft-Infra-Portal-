"""Reference-data tables for the Infra Portal (increment 1.1).

Scope is deliberately limited to the *reference data* the portal reads:
projects, cost centres, technologies, existing environments, sizing anchors,
and rate cards (ARCHITECTURE.md §5). The transactional tables (request,
estimate, approval, registry, audit_log) are added in the later increments
that first use them.

Column types are kept portable (no Postgres-only types) so the same models run
against an in-memory database in the tests.
"""

from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.session import Base


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
