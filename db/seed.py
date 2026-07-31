"""Create the reference-data tables and fill them with starter data.

Run it inside the API container:
    docker compose exec api python -m db.seed

Safe to re-run: it only inserts rows that aren't already there, then prints a
summary so you can see what's in the database.

All data here is mock/demo reference data — no real systems are touched.
"""

from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from db.models import (
    CostCentre,
    Environment,
    Project,
    RateCard,
    SizingAnchor,
    Subsidiary,
    Technology,
)
from db.session import Base, SessionLocal, engine

PROJECTS = [
    {"code": "EGATE", "name": "eGate Modernisation"},
    {"code": "VISA", "name": "Visa Platform"},
    {"code": "BIO", "name": "Biometric Services"},
]

COST_CENTRES = [
    {"code": "IMD-1001", "name": "Infrastructure Management"},
    {"code": "IMD-2002", "name": "Border Systems"},
    {"code": "IMD-3003", "name": "Identity & Biometrics"},
]

# Placeholder subsidiaries — adjust the codes/names to your real org structure.
SUBSIDIARIES = [
    {"code": "EMRTECH", "name": "emaratech"},
    {"code": "GDRFAD", "name": "GDRFA Dubai"},
    {"code": "ICP", "name": "ICP"},
]

TECHNOLOGIES = [
    {"code": "postgres16", "name": "PostgreSQL 16", "lifecycle_state": "certified"},
    {"code": "redis7", "name": "Redis 7", "lifecycle_state": "certified"},
    {"code": "nginx", "name": "NGINX", "lifecycle_state": "certified"},
    {"code": "k8s", "name": "Kubernetes", "lifecycle_state": "preview"},
    {"code": "rhel9", "name": "RHEL 9 VM", "lifecycle_state": "certified"},
    {"code": "win2019", "name": "Windows Server 2019", "lifecycle_state": "deprecated"},
    # Catalog expansion (increment 6.2, from the UX brief). Sizing is uniform
    # per size (below); a few carry a software licence (see RATE_CARDS +
    # api/pricing.TECHNOLOGY_LICENCE). Real per-technology provisioning is still
    # deferred — an apply creates the same placeholder resource, tagged.
    {"code": "oracle-db", "name": "Oracle Database 19c", "lifecycle_state": "certified"},
    {"code": "mssql", "name": "SQL Server 2022", "lifecycle_state": "certified"},
    {"code": "mongodb", "name": "MongoDB 7", "lifecycle_state": "certified"},
    {"code": "kafka", "name": "Apache Kafka", "lifecycle_state": "certified"},
    {"code": "rabbitmq", "name": "RabbitMQ", "lifecycle_state": "certified"},
    {"code": "elasticsearch", "name": "Elasticsearch 8", "lifecycle_state": "certified"},
    {"code": "opensearch", "name": "OpenSearch 2", "lifecycle_state": "preview"},
    {"code": "java21", "name": "Java 21 (JVM)", "lifecycle_state": "certified"},
    {"code": "dotnet8", "name": ".NET 8", "lifecycle_state": "certified"},
    {"code": "nodejs20", "name": "Node.js 20", "lifecycle_state": "certified"},
    {"code": "python312", "name": "Python 3.12", "lifecycle_state": "certified"},
    {"code": "apache", "name": "Apache HTTP Server", "lifecycle_state": "certified"},
    {"code": "openshift", "name": "OpenShift", "lifecycle_state": "preview"},
    {"code": "vault", "name": "HashiCorp Vault", "lifecycle_state": "certified"},
    {"code": "keycloak", "name": "Keycloak", "lifecycle_state": "certified"},
    # A first-class compute (VM) type — a stoppable OCI Compute instance, which
    # the control plane can stop/start. See COMPUTE_CODES below.
    {"code": "compute-vm", "name": "Compute Instance (VM)", "lifecycle_state": "certified"},
]

# Technologies that provision a stoppable OCI Compute instance (oci-instance)
# rather than the default object-storage bucket. RHEL/Windows are already VMs.
COMPUTE_CODES = {"compute-vm", "rhel9", "win2019"}

# size -> (vcpu, memory_gb, storage_gb), applied to every technology.
SIZES = {
    "small": (2, 4, 50),
    "medium": (4, 16, 200),
    "large": (8, 64, 500),
    "xlarge": (16, 128, 1000),
}

RATE_CARDS = [
    # On-prem: monthly resource rates + a one-time setup fee per component.
    {"kind": "onprem", "item": "vcpu", "unit": "per vCPU/month", "rate": 45.0},
    {"kind": "onprem", "item": "memory-gb", "unit": "per GB/month", "rate": 12.0},
    {"kind": "onprem", "item": "storage-gb", "unit": "per GB/month", "rate": 1.5},
    {"kind": "onprem", "item": "setup", "unit": "one-time per component", "rate": 500.0},
    # Software licences (independent of deployment target), charged monthly.
    {"kind": "licence", "item": "postgres-licence", "unit": "per month", "rate": 0.0},
    {"kind": "licence", "item": "windows-licence", "unit": "per month", "rate": 320.0},
    # Commercial database licences (increment 6.2).
    {"kind": "licence", "item": "oracle-licence", "unit": "per month", "rate": 1800.0},
    {"kind": "licence", "item": "mssql-licence", "unit": "per month", "rate": 700.0},
    # Azure: compute per hour, storage per GB/month; 20% negotiated discount.
    {"kind": "cloud_azure", "item": "vcpu-hour", "unit": "per vCPU/hour", "rate": 0.14,
     "discount_pct": 20.0},
    {"kind": "cloud_azure", "item": "memory-gb-hour", "unit": "per GB/hour", "rate": 0.012,
     "discount_pct": 20.0},
    {"kind": "cloud_azure", "item": "storage-gb-month", "unit": "per GB/month", "rate": 0.10,
     "discount_pct": 20.0},
    # OCI: compute per hour, storage per GB/month; 15% negotiated discount.
    {"kind": "cloud_oci", "item": "vcpu-hour", "unit": "per vCPU/hour", "rate": 0.11,
     "discount_pct": 15.0},
    {"kind": "cloud_oci", "item": "memory-gb-hour", "unit": "per GB/hour", "rate": 0.009,
     "discount_pct": 15.0},
    {"kind": "cloud_oci", "item": "storage-gb-month", "unit": "per GB/month", "rate": 0.08,
     "discount_pct": 15.0},
]


def _upsert_by(session: Session, model, match_field: str, rows: list[dict]) -> None:
    """Insert each row only if a row with the same match_field isn't present."""
    for row in rows:
        exists = session.scalar(
            select(model).where(getattr(model, match_field) == row[match_field])
        )
        if exists is None:
            session.add(model(**row))


def seed(session: Session) -> None:
    """Insert all reference data (idempotent)."""
    _upsert_by(session, Project, "code", PROJECTS)
    _upsert_by(session, CostCentre, "code", COST_CENTRES)
    _upsert_by(session, Subsidiary, "code", SUBSIDIARIES)
    _upsert_by(session, Technology, "code", TECHNOLOGIES)
    session.flush()  # flush new technologies (e.g. compute-vm) BEFORE the update
    # below, so a freshly-inserted compute type is classified too (the app session
    # has autoflush off, so the Core UPDATE wouldn't see the pending insert).
    # Classify the compute (VM) technologies so they provision a stoppable
    # instance. An update (not just insert) so existing rows are corrected too.
    session.execute(
        update(Technology).where(Technology.code.in_(COMPUTE_CODES))
        .values(resource_kind="oci-instance")
    )
    session.flush()  # projects & technologies now have ids

    # Existing environments (need a project id).
    egate = session.scalar(select(Project).where(Project.code == "EGATE"))
    visa = session.scalar(select(Project).where(Project.code == "VISA"))
    environments = [
        {"name": "egate-prod", "environment_class": "prod", "project_id": egate.id},
        {"name": "visa-uat", "environment_class": "non-prod", "project_id": visa.id},
    ]
    _upsert_by(session, Environment, "name", environments)

    # Sizing anchors: one per technology per size (F-CAT-07).
    technologies = session.scalars(select(Technology)).all()
    for tech in technologies:
        for size, (vcpu, mem, storage) in SIZES.items():
            exists = session.scalar(
                select(SizingAnchor).where(
                    SizingAnchor.technology_id == tech.id,
                    SizingAnchor.size == size,
                )
            )
            if exists is None:
                session.add(
                    SizingAnchor(
                        technology_id=tech.id,
                        size=size,
                        vcpu=vcpu,
                        memory_gb=mem,
                        storage_gb=storage,
                        effective_from=date(2026, 1, 1),
                        version=1,
                    )
                )

    # Rate cards (F-FIN-04): match on item+kind so re-runs don't duplicate.
    for row in RATE_CARDS:
        exists = session.scalar(
            select(RateCard).where(
                RateCard.kind == row["kind"], RateCard.item == row["item"]
            )
        )
        if exists is None:
            session.add(RateCard(**row))

    session.commit()


def _print_summary(session: Session) -> None:
    counts = {
        "projects": session.scalar(select(func.count()).select_from(Project)),
        "cost_centres": session.scalar(select(func.count()).select_from(CostCentre)),
        "technologies": session.scalar(select(func.count()).select_from(Technology)),
        "environments": session.scalar(select(func.count()).select_from(Environment)),
        "sizing_anchors": session.scalar(select(func.count()).select_from(SizingAnchor)),
        "rate_cards": session.scalar(select(func.count()).select_from(RateCard)),
    }
    print("Seed complete. Row counts:")
    for name, n in counts.items():
        print(f"  {name:>14}: {n}")


def main() -> None:
    # Create the reference-data tables if they don't exist yet.
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        seed(session)
        _print_summary(session)


if __name__ == "__main__":
    main()
