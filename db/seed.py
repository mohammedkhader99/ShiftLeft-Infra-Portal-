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
    TechnologyDelivery,
    CostCentre,
    Environment,
    Project,
    RateCard,
    ComponentOption,
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
# How each catalogue entry is delivered (F-CAT). Replaces a rule that read the
# technology CODE: anything not prefixed oci-/aws-/azure-/gcp- was assumed to be
# software you install on a machine, which called OCI's managed PostgreSQL
# "software" and "Backup & Recovery" installable. REQ-2026-0183 spent a real
# machine learning the second one.
#
# Anything absent from here falls back to that old guess, so a technology added
# tomorrow still works — it is just guessed at rather than known.
DELIVERY = {
    # --- the cloud runs it; we consume an endpoint -------------------------
    "oci-objectstorage": ("managed", "OCI Object Storage — a bucket, billed per GB."),
    "oci-oke": ("managed", "OCI Container Engine for Kubernetes — a managed control plane."),
    "oci-adb": ("managed", "Oracle Autonomous Database — fully managed by OCI."),
    "oci-functions": ("managed", "OCI Functions — serverless; there is no machine."),
    "postgres16": ("managed",
                   "OCI Database with PostgreSQL — the MANAGED service, not PostgreSQL "
                   "installed on a VM. The plain name hides that, which is why it is "
                   "recorded here rather than inferred."),
    "aws-s3": ("managed", "Amazon S3."),
    "aws-rds": ("managed", "Amazon RDS — managed relational database."),
    "aws-lambda": ("managed", "AWS Lambda — serverless."),
    "aws-dynamodb": ("managed", "Amazon DynamoDB."),
    "aws-eks": ("managed", "Amazon EKS — managed Kubernetes."),
    "azure-blob": ("managed", "Azure Blob Storage."),
    "azure-sql": ("managed", "Azure SQL Database."),
    "azure-functions": ("managed", "Azure Functions — serverless."),
    "azure-aks": ("managed", "Azure Kubernetes Service."),
    "azure-cosmos": ("managed", "Azure Cosmos DB."),
    "gcp-gcs": ("managed", "Google Cloud Storage."),
    "gcp-cloudsql": ("managed", "Google Cloud SQL."),
    "gcp-functions": ("managed", "Google Cloud Functions — serverless."),
    "gcp-gke": ("managed", "Google Kubernetes Engine."),
    "gcp-firestore": ("managed", "Firestore."),

    # --- a bare machine; nothing is installed on it ------------------------
    "compute-vm": ("machine", "A virtual machine with no software installed."),
    "rhel9": ("machine", "A Red Hat Enterprise Linux 9 machine."),
    "win2019": ("machine", "A Windows Server 2019 machine."),

    # --- an outcome, not an installable thing ------------------------------
    #
    # These have no package, no archive and no cloud resource. A requester
    # picking one is asking for a capability the platform team designs and
    # builds; the portal must say so rather than guess at a package name.
    "backup": ("capability",
               "Backup and recovery is a capability, not a package — there is no "
               "`backup` to install. Ask the infrastructure team what it should "
               "protect and how often."),
    "logging": ("capability",
                "Centralised logging is a capability. The portal can build the "
                "components it runs on (Elasticsearch, OpenSearch) but not "
                "\"logging\" itself."),
    "monitoring": ("capability",
                   "Monitoring and alerting is a capability, not a package."),
    "api-gateway": ("capability",
                    "An API gateway is a capability. NGINX can serve as one and is "
                    "installable; this entry is not."),
    "service-mesh": ("capability",
                     "A service mesh (Istio) is installed INTO a Kubernetes cluster, "
                     "not onto a bare machine. Request OCI Container Engine (OKE) "
                     "first."),
    "k8s": ("capability",
            "Generic Kubernetes. On OCI the buildable thing is OCI Container Engine "
            "(oci-oke) — request that instead."),
}
# Everything else in the catalogue is software installed on a machine the
# customer owns: nginx, keycloak, kafka, oracle-db, mssql and their like.

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
    # --- Platform-service add-ons (available on every deployment target) -------
    {"code": "monitoring", "name": "Monitoring & Alerting", "lifecycle_state": "certified"},
    {"code": "logging", "name": "Centralised Logging", "lifecycle_state": "certified"},
    {"code": "backup", "name": "Backup & Recovery", "lifecycle_state": "certified"},
    {"code": "service-mesh", "name": "Service Mesh (Istio)", "lifecycle_state": "preview"},
    {"code": "api-gateway", "name": "API Gateway", "lifecycle_state": "certified"},
    # --- AWS-managed services (targets: aws) ----------------------------------
    {"code": "aws-rds", "name": "Amazon RDS", "lifecycle_state": "certified", "targets": "aws"},
    {"code": "aws-s3", "name": "Amazon S3", "lifecycle_state": "certified", "targets": "aws"},
    {"code": "aws-eks", "name": "Amazon EKS", "lifecycle_state": "certified", "targets": "aws"},
    {"code": "aws-lambda", "name": "AWS Lambda", "lifecycle_state": "certified", "targets": "aws"},
    {"code": "aws-dynamodb", "name": "Amazon DynamoDB", "lifecycle_state": "certified", "targets": "aws"},
    # --- Azure-managed services (targets: azure) ------------------------------
    {"code": "azure-sql", "name": "Azure SQL Database", "lifecycle_state": "certified", "targets": "azure"},
    {"code": "azure-blob", "name": "Azure Blob Storage", "lifecycle_state": "certified", "targets": "azure"},
    {"code": "azure-aks", "name": "Azure Kubernetes Service (AKS)", "lifecycle_state": "certified", "targets": "azure"},
    {"code": "azure-functions", "name": "Azure Functions", "lifecycle_state": "certified", "targets": "azure"},
    {"code": "azure-cosmos", "name": "Azure Cosmos DB", "lifecycle_state": "certified", "targets": "azure"},
    # --- OCI-managed services (targets: oci) ----------------------------------
    {"code": "oci-adb", "name": "Oracle Autonomous Database", "lifecycle_state": "certified", "targets": "oci"},
    {"code": "oci-objectstorage", "name": "OCI Object Storage", "lifecycle_state": "certified", "targets": "oci"},
    {"code": "oci-oke", "name": "OCI Container Engine (OKE)", "lifecycle_state": "certified", "targets": "oci"},
    {"code": "oci-functions", "name": "OCI Functions", "lifecycle_state": "certified", "targets": "oci"},
    # --- GCP-managed services (targets: gcp) ----------------------------------
    {"code": "gcp-cloudsql", "name": "Google Cloud SQL", "lifecycle_state": "certified", "targets": "gcp"},
    {"code": "gcp-gcs", "name": "Google Cloud Storage", "lifecycle_state": "certified", "targets": "gcp"},
    {"code": "gcp-gke", "name": "Google Kubernetes Engine (GKE)", "lifecycle_state": "certified", "targets": "gcp"},
    {"code": "gcp-functions", "name": "Google Cloud Functions", "lifecycle_state": "certified", "targets": "gcp"},
    {"code": "gcp-firestore", "name": "Firestore", "lifecycle_state": "certified", "targets": "gcp"},
]

# Technologies that provision a stoppable OCI Compute instance (oci-instance)
# rather than the default object-storage bucket. RHEL/Windows are already VMs.
COMPUTE_CODES = {"compute-vm", "rhel9", "win2019"}

# Technologies delivered as a managed OCI Database with PostgreSQL system
# (oci-postgres) — the requester gets an actual database, not a placeholder
# bucket (GAP-ANALYSIS.md step 2). Real provisioning stays double-gated in the
# orchestrator (OCI_PSQL_ENABLED + the subnet/vault-secret inputs).
POSTGRES_CODES = {"postgres16"}

# size -> (vcpu, memory_gb, storage_gb), applied to every technology.
SIZES = {
    "small": (2, 4, 50),
    "medium": (4, 16, 200),
    "large": (8, 64, 500),
    "xlarge": (16, 128, 1000),
}

# Versions offered on the component detail form: {code: [(value, is_default)]}.
#
# This list is deliberately SHORT. A version dropdown the machine ignores is the
# Redis-6-sold-as-7 bug with a menu in front of it, so a version appears here
# only where the platform has a proven way to deliver it — today, the Oracle
# Linux 9 dnf module streams verified in GAP-ANALYSIS step 9, plus packages whose
# name already pins the version. A technology absent from this map simply gets no
# version dropdown; that is the honest answer, not an omission.
#
# The hourly cloud-option fetch (next increment) adds rows with source="oci-live"
# alongside these, and replaces only its own.
COMPONENT_VERSIONS = {
    # dnf module streams available on OL9 — a genuine choice.
    "nginx": [("1.20", True), ("1.22", False), ("1.24", False)],
    # The catalogue entry is "Redis 7"; the redis:7 stream is what delivers it.
    # Offering 6 here would contradict the name of the thing being requested.
    "redis7": [("7", True)],
    # OL9 ships one httpd, with no module streams. One honest option.
    "apache": [("2.4", True)],
    # Version-pinned package names — the version is the package.
    "java21": [("21", True)],
    "python312": [("3.12", True)],
    # nodejs:20 stream; OL9's default stream is 18, so the stream is what makes
    # the catalogue's "Node.js 20" true.
    "nodejs20": [("20", True)],
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
    # Rates for the OCI resource kinds that are NOT virtual machines. List
    # prices in AED, read from Oracle's public price list on 2026-08-21; the
    # oci_pricing adapter refreshes them live when OCI_PRICING_MODE=live.
    # Before these existed a bucket was billed a VM's compute (F-FIN).
    #   B91628  Object Storage - Storage                  per GB/month, first 10 GB free
    #   B96545  OCI Kubernetes Engine - Enhanced Cluster  per cluster/hour
    #   B99060  Database with PostgreSQL - X86            per OCPU/hour (= 2 vCPU)
    {"kind": "cloud_oci", "item": "bucket-storage-gb-month", "unit": "per GB/month",
     "rate": 0.0936615, "discount_pct": 15.0},
    # An ALLOWANCE, not a price: never discounted (pricing.NOT_A_RATE).
    {"kind": "cloud_oci", "item": "bucket-free-gb", "unit": "GB free per month",
     "rate": 10.0, "discount_pct": 0.0},
    {"kind": "cloud_oci", "item": "oke-cluster-hour", "unit": "per cluster/hour",
     "rate": 0.3673, "discount_pct": 15.0},
    {"kind": "cloud_oci", "item": "psql-vcpu-hour", "unit": "per vCPU/hour",
     "rate": 0.179977, "discount_pct": 15.0},
    # AWS: compute per hour, storage per GB/month; 10% negotiated discount
    # (indicative rates; live via the AWS Price List API, aws_pricing adapter).
    {"kind": "cloud_aws", "item": "vcpu-hour", "unit": "per vCPU/hour", "rate": 0.13,
     "discount_pct": 10.0},
    {"kind": "cloud_aws", "item": "memory-gb-hour", "unit": "per GB/hour", "rate": 0.011,
     "discount_pct": 10.0},
    {"kind": "cloud_aws", "item": "storage-gb-month", "unit": "per GB/month", "rate": 0.11,
     "discount_pct": 10.0},
    # GCP: compute per hour, storage per GB/month; 12% negotiated discount
    # (indicative rates; live via the GCP Cloud Billing Catalog, gcp_pricing adapter).
    {"kind": "cloud_gcp", "item": "vcpu-hour", "unit": "per vCPU/hour", "rate": 0.12,
     "discount_pct": 12.0},
    {"kind": "cloud_gcp", "item": "memory-gb-hour", "unit": "per GB/hour", "rate": 0.010,
     "discount_pct": 12.0},
    {"kind": "cloud_gcp", "item": "storage-gb-month", "unit": "per GB/month", "rate": 0.09,
     "discount_pct": 12.0},
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
    # Delivery model per technology. Anything not listed keeps the old
    # code-name guess, so a technology added tomorrow still works.
    _upsert_by(session, TechnologyDelivery, "technology_code",
               [{"technology_code": code, "delivery_model": model, "note": note}
                for code, (model, note) in DELIVERY.items()])
    session.flush()  # flush new technologies (e.g. compute-vm) BEFORE the update
    # below, so a freshly-inserted compute type is classified too (the app session
    # has autoflush off, so the Core UPDATE wouldn't see the pending insert).
    # Classify the compute (VM) technologies so they provision a stoppable
    # instance. An update (not just insert) so existing rows are corrected too.
    session.execute(
        update(Technology).where(Technology.code.in_(COMPUTE_CODES))
        .values(resource_kind="oci-instance")
    )
    # Managed PostgreSQL: delivered as a real database system, not a bucket.
    session.execute(
        update(Technology).where(Technology.code.in_(POSTGRES_CODES))
        .values(resource_kind="oci-postgres")
    )
    session.flush()  # projects & technologies now have ids

    # Existing environments (need a project id).
    egate = session.scalar(select(Project).where(Project.code == "EGATE"))
    visa = session.scalar(select(Project).where(Project.code == "VISA"))
    environments = [
        {"name": "egate-prod", "environment_class": "Production", "project_id": egate.id},
        {"name": "visa-uat", "environment_class": "UAT", "project_id": visa.id},
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

    # Version options for the component detail form. Match on the whole natural
    # key so re-runs don't duplicate, and so a version withdrawn from the map
    # above is left in place rather than silently deleted — removing an offered
    # version is a catalogue decision, not a side effect of re-seeding.
    for code, versions in COMPONENT_VERSIONS.items():
        for order, (value, is_default) in enumerate(versions):
            exists = session.scalar(
                select(ComponentOption).where(
                    ComponentOption.deployment_target == "",
                    ComponentOption.technology_code == code,
                    ComponentOption.field == "version",
                    ComponentOption.value == value,
                )
            )
            if exists is None:
                session.add(ComponentOption(
                    deployment_target="", technology_code=code, field="version",
                    value=value, label=value, is_default=is_default,
                    sort_order=order, source="seed",
                ))

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
