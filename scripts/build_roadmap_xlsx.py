"""Build the high-level project plan workbook for management review.

Grounded in the portal's real state on 2026-08-21, not in intentions:
certified blueprints and catalogue counts are read from the running database
(see the CURRENT STATE sheet, which cites the request references that prove
each one). Dates are ESTIMATES and are marked as such — they are the one part
of this workbook nobody has verified.
"""

from __future__ import annotations

import datetime as dt
import pathlib

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = pathlib.Path(__file__).resolve().parents[1] / "Infra-Portal-Roadmap.xlsx"

FONT = "Arial"
NAVY = "1F3864"
BLUE = "2E5C8A"
LIGHT = "D9E2F3"
GREY = "F2F2F2"
GREEN = "C6EFCE"
AMBER = "FFEB9C"
RED = "FFC7CE"
INPUT_BLUE = "0000FF"

THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def title(ws, text, subtitle, width):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    c = ws.cell(row=1, column=1, value=text)
    c.font = Font(name=FONT, size=15, bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=width)
    s = ws.cell(row=2, column=2 - 1, value=subtitle)
    s.font = Font(name=FONT, size=9, italic=True, color="595959")
    s.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[2].height = 18


def header(ws, row, labels, widths):
    for i, (label, width) in enumerate(zip(labels, widths), start=1):
        c = ws.cell(row=row, column=i, value=label)
        c.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=BLUE)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BOX
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[row].height = 30
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


STATUS_FILL = {
    "Complete": GREEN,
    "In progress": AMBER,
    "Not started": GREY,
    "Blocked": RED,
    "Proposed": GREY,
}


def write_row(ws, row, values, status_col=None, wrap_cols=()):
    for i, v in enumerate(values, start=1):
        c = ws.cell(row=row, column=i, value=v)
        c.font = Font(name=FONT, size=10)
        c.border = BOX
        c.alignment = Alignment(vertical="top",
                                wrap_text=(i in wrap_cols),
                                horizontal="left")
        if status_col and i == status_col and v in STATUS_FILL:
            c.fill = PatternFill("solid", fgColor=STATUS_FILL[v])
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.font = Font(name=FONT, size=10, bold=True)


def note(ws, row, text, width, fill=AMBER):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(name=FONT, size=9, italic=True)
    c.fill = PatternFill("solid", fgColor=fill)
    c.alignment = Alignment(horizontal="left", vertical="center",
                            wrap_text=True, indent=1)
    ws.row_dimensions[row].height = 30


wb = Workbook()

# =============================================================== 1. ROADMAP ===
ws = wb.active
ws.title = "Roadmap"
title(ws, "Shift-Left Infrastructure Provisioning Portal — High-Level Plan",
      "OCI workloads first, then Azure.  Prepared 2026-08-21.  "
      "Dates in blue are ESTIMATES for review — everything else is measured from the running system.",
      8)
header(ws, 4,
       ["Phase", "Stream", "Objective", "Start", "End",
        "Weeks", "Status", "Key outcome / proof"],
       [8, 12, 42, 12, 12, 8, 13, 46])

PHASES = [
    # Dates for P1-P5 are TAKEN FROM GIT HISTORY, not estimated: first commit
    # 2026-07-26, 263 commits to 2026-08-20.
    ("P1", "Platform", "Portal, API, Entra sign-in, Jira approvals, OPA, orchestrator, audit",
     dt.date(2026, 7, 26), dt.date(2026, 8, 3), "Complete",
     "Request-to-provision spine live. Real OCI apply/destroy behind a two-step gate by day 3."),
    ("P2", "OCI", "Core OCI workloads: Object Storage, Compute VM, middleware, runtimes",
     dt.date(2026, 7, 28), dt.date(2026, 8, 16), "Complete",
     "Apache, nginx, Redis 7, Java 21, Python 3.12, Node 20 — each certified on measured evidence"),
    ("P3", "OCI", "Kubernetes (OKE) end to end from the portal",
     dt.date(2026, 8, 13), dt.date(2026, 8, 17), "Complete",
     "REQ-2026-0153 built a cluster in 15 minutes, unattended"),
    ("P4", "OCI", "Managed database (PostgreSQL 16) end to end",
     dt.date(2026, 8, 17), dt.date(2026, 8, 20), "Complete",
     "REQ-2026-0159 ACTIVE. Version and shape resolved from OCI at build time, not hard-coded"),
    ("P5", "Platform", "Self-certifying catalogue (C0-C4): remove manual certification",
     dt.date(2026, 8, 20), dt.date(2026, 10, 9), "In progress",
     "C0 shipped 20 Aug. Certification earned by a proof build and revoked automatically on failure"),
    ("P6", "OCI", "Remaining OCI workloads: ADB, Oracle DB 19c, Kafka, data protection",
     dt.date(2026, 10, 12), dt.date(2026, 11, 27), "Not started",
     "Full OCI catalogue buildable, each item backed by a proof build"),
    ("P7", "Azure", "Azure foundation: subscriptions, identity, landing zone, policy",
     dt.date(2026, 10, 12), dt.date(2026, 12, 4), "Not started",
     "CRITICAL PATH — depends on cloud, identity and network teams, so it runs parallel to P6"),
    ("P8", "Azure", "First Azure workloads: Storage Account, Virtual Machine, middleware",
     dt.date(2026, 12, 7), dt.date(2027, 1, 22), "Not started",
     "Same request form and approval path; deployment target = Azure"),
    ("P9", "Azure", "Azure managed services: Azure SQL, PostgreSQL Flexible, AKS, Key Vault",
     dt.date(2027, 1, 25), dt.date(2027, 3, 26), "Not started",
     "Managed data and Kubernetes parity with OCI"),
    ("P10", "Both", "Multi-cloud operations: cost comparison, drift, DR posture, reporting",
     dt.date(2027, 3, 29), dt.date(2027, 5, 21), "Not started",
     "One portal, one approval path, two clouds, comparable cost reporting"),
]

r = 5
for pid, stream, obj, start, end, status, outcome in PHASES:
    write_row(ws, r, [pid, stream, obj, start, end, None, status, outcome],
              status_col=7, wrap_cols=(3, 8))
    ws.cell(row=r, column=6).value = f"=ROUND((E{r}-D{r})/7,1)"
    for col in (4, 5):
        ws.cell(row=r, column=col).number_format = "dd-mmm-yy"
        ws.cell(row=r, column=col).font = Font(name=FONT, size=10, color=INPUT_BLUE)
        ws.cell(row=r, column=col).alignment = Alignment(horizontal="center")
    ws.cell(row=r, column=6).alignment = Alignment(horizontal="center")
    ws.row_dimensions[r].height = 30
    r += 1

first, last = 5, r - 1
r += 1
ws.cell(row=r, column=1, value="Totals").font = Font(name=FONT, size=10, bold=True)
# Plain & concatenation, not CONCAT: this environment has no LibreOffice, so a
# post-2007 function could not be verified before shipping. & is universal.
ws.cell(row=r, column=3,
        value=f'=COUNTIF(G{first}:G{last},"Complete")&" of "'
              f'&COUNTA(A{first}:A{last})&" phases complete"'
        ).font = Font(name=FONT, size=10, bold=True)
ws.cell(row=r, column=4, value=f"=MIN(D{first}:D{last})").number_format = "dd-mmm-yy"
ws.cell(row=r, column=5, value=f"=MAX(E{first}:E{last})").number_format = "dd-mmm-yy"
ws.cell(row=r, column=6, value=f"=ROUND((MAX(E{first}:E{last})-MIN(D{first}:D{last}))/7,1)")
for col in (1, 3, 4, 5, 6):
    ws.cell(row=r, column=col).font = Font(name=FONT, size=10, bold=True)
    ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor=LIGHT)
    ws.cell(row=r, column=col).border = BOX
ws.cell(row=r, column=4).alignment = Alignment(horizontal="center")
ws.cell(row=r, column=5).alignment = Alignment(horizontal="center")
ws.cell(row=r, column=6).alignment = Alignment(horizontal="center")

note(ws, r + 2,
     "P1-P5 dates are FACT, read from git history (first commit 2026-07-26; 263 commits to "
     "2026-08-20). P6-P10 are ESTIMATES based on that observed pace and assume the current "
     "team size. Adjust any blue cell; Weeks and Totals recalculate.", 8)
note(ws, r + 3,
     "Azure foundation (P7) runs PARALLEL to the last OCI phase (P6) on purpose: it depends on "
     "the cloud, identity and network teams rather than on this team, so it is the long pole. "
     "Starting it late would delay everything after it. Build work does not overlap.", 8, LIGHT)

# ========================================================== 2. OCI WORKLOADS ===
ws = wb.create_sheet("OCI Workloads")
title(ws, "OCI Workload Provisioning — status by workload",
      "Status reflects what the portal can actually build today, proven by a real request where stated.", 7)
header(ws, 4,
       ["#", "Workload", "Service type", "Status", "Proof / evidence",
        "Est. effort (wks)", "Notes"],
       [5, 30, 20, 13, 34, 14, 44])

OCI_WORKLOADS = [
    ("Object Storage bucket", "Storage (managed)", "Complete",
     "Certified; built by multiple requests", 0,
     "The original safe default. Versioning + optional customer-managed key."),
    ("Compute VM (Linux)", "IaaS", "Complete",
     "Certified; boot self-report verified", 0,
     "Private-only by design. First-boot config renders per OS family."),
    ("Apache HTTP Server", "Middleware on VM", "Complete",
     "Certified on evidence (Oracle Linux)", 0, "Version measured on the machine, not assumed."),
    ("nginx", "Middleware on VM", "Complete",
     "Certified; version list sourced from OS", 0, "Offered versions come from the OS module streams."),
    ("Redis 7", "Data (on VM)", "Complete", "Certified (RHEL + Ubuntu)", 0, ""),
    ("Java 21 / Python 3.12 / Node 20", "Runtime on VM", "Complete",
     "Certified; measured on real machines", 0,
     "Node 20 is Oracle-Linux-only — Ubuntu ships Node 18, so it is refused rather than substituted."),
    ("OKE (Kubernetes)", "Managed Kubernetes", "Complete",
     "REQ-2026-0153 — 15 min, unattended", 0,
     "Consumes network-team subnets. Worker image resolved from OCI by name."),
    ("PostgreSQL 16 (managed)", "Managed database", "Complete",
     "REQ-2026-0159 — ACTIVE, version 16", 0,
     "Shape and version resolved from OCI. Admin password held in OCI Vault, never by the portal."),
    ("DNS record", "Networking", "Complete",
     "Blueprint shipped, shares environment lifecycle", 0, "Record is destroyed with the environment it names."),
    ("Kafka", "Middleware", "In progress", "Blueprint exists; not yet certified", 2,
     "Needs a proof build to earn certification."),
    ("Oracle Autonomous Database", "Managed database", "Not started",
     "Catalogue placeholder only", 4,
     "Currently maps to a bucket placeholder — must not be certified until a real module exists."),
    ("Oracle Database 19c", "Database", "Not started",
     "Catalogue placeholder only", 6,
     "DECISION NEEDED: Base Database VM, Exadata, or self-installed. Licensing differs materially."),
    ("Object lifecycle / backup policy", "Data protection", "Not started", "", 3,
     "Retention and restore paths for the workloads above."),
]

r = 5
for i, (name, kind, status, proof, effort, notes) in enumerate(OCI_WORKLOADS, start=1):
    write_row(ws, r, [i, name, kind, status, proof, effort if effort else "—", notes],
              status_col=4, wrap_cols=(2, 5, 7))
    ws.cell(row=r, column=6).alignment = Alignment(horizontal="center")
    ws.row_dimensions[r].height = 30
    r += 1

ofirst, olast = 5, r - 1
r += 1
ws.cell(row=r, column=2, value="Workloads complete").font = Font(name=FONT, size=10, bold=True)
ws.cell(row=r, column=4, value=f'=COUNTIF(D{ofirst}:D{olast},"Complete")&" of "&COUNTA(B{ofirst}:B{olast})'
        ).font = Font(name=FONT, size=10, bold=True)
ws.cell(row=r, column=6, value=f"=SUM(F{ofirst}:F{olast})").font = Font(name=FONT, size=10, bold=True)
ws.cell(row=r, column=6).alignment = Alignment(horizontal="center")
for col in (2, 4, 6):
    ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor=LIGHT)
    ws.cell(row=r, column=col).border = BOX

note(ws, r + 2,
     "'Complete' means a real request built the thing and the portal verified it — not that "
     "code exists. Effort estimates exclude the proof build itself (see Risks, R2).", 7)

# ======================================================== 3. AZURE WORKLOADS ===
ws = wb.create_sheet("Azure Workloads")
title(ws, "Azure Workload Provisioning — planned",
      "Nothing is buildable on Azure today. The catalogue LISTS 32 Azure technologies; zero are certified.", 7)
header(ws, 4,
       ["#", "Workload / capability", "Service type", "Status", "Depends on",
        "Est. effort (wks)", "Notes"],
       [5, 32, 20, 13, 26, 14, 44])

AZURE_ITEMS = [
    ("Subscription + management group design", "Foundation", "Not started",
     "Cloud/network team", 2,
     "Which subscriptions per environment tier, and who owns them."),
    ("Entra ID app registration + RBAC", "Identity", "Not started",
     "Identity team", 2,
     "Portal already authenticates users against Entra; this is the SERVICE principal that provisions."),
    ("Landing zone: VNets, subnets per tier", "Network", "Not started",
     "Network team", 4,
     "Mirrors the OCI model — one network per environment tier, provided to the portal, not created by it."),
    ("Azure Policy baseline + tagging", "Governance", "Not started",
     "Security team", 3,
     "Equivalent of the OPA policy and tagging already enforced on OCI."),
    ("Terraform provider + state wiring", "Platform", "Not started",
     "Foundation above", 2,
     "Orchestrator already runs Terraform; this adds the azurerm provider and credentials path."),
    ("Cost model / rate card", "FinOps", "Complete",
     "Already delivered", 0,
     "Azure pricing is already implemented and used for estimates."),
    ("Storage Account", "Storage (managed)", "Not started", "Foundation", 2,
     "First Azure workload — the equivalent of the OCI bucket."),
    ("Virtual Machine (Linux)", "IaaS", "Not started", "Landing zone", 3,
     "Reuses the same first-boot configuration engine as OCI VMs."),
    ("Middleware on VM (nginx, Redis, runtimes)", "Middleware", "Not started",
     "Azure VM", 3,
     "Should be largely free — the config engine is cloud-agnostic."),
    ("Azure SQL Database", "Managed database", "Not started", "Foundation", 4,
     "Already a catalogue entry with no implementation."),
    ("PostgreSQL Flexible Server", "Managed database", "Not started", "Foundation", 3,
     "Closest analogue to the delivered OCI PostgreSQL work."),
    ("AKS (Kubernetes)", "Managed Kubernetes", "Not started", "Landing zone", 5,
     "Expect the same class of issues found in OKE: image, version and node-registration."),
    ("Key Vault integration", "Security", "Not started", "Identity", 2,
     "Equivalent of the OCI Vault pattern — the portal holds a secret REFERENCE, never a password."),
    ("Azure DNS", "Networking", "Not started", "Landing zone", 2, ""),
]

r = 5
for i, (name, kind, status, dep, effort, notes) in enumerate(AZURE_ITEMS, start=1):
    write_row(ws, r, [i, name, kind, status, dep, effort if effort else "—", notes],
              status_col=4, wrap_cols=(2, 5, 7))
    ws.cell(row=r, column=6).alignment = Alignment(horizontal="center")
    ws.row_dimensions[r].height = 30
    r += 1

afirst, alast = 5, r - 1
r += 1
ws.cell(row=r, column=2, value="Total estimated effort (weeks)").font = Font(name=FONT, size=10, bold=True)
ws.cell(row=r, column=6, value=f"=SUM(F{afirst}:F{alast})").font = Font(name=FONT, size=10, bold=True)
ws.cell(row=r, column=6).alignment = Alignment(horizontal="center")
for col in (2, 6):
    ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor=LIGHT)
    ws.cell(row=r, column=col).border = BOX

note(ws, r + 2,
     "Effort is sequential-weeks of build, NOT elapsed time. Items 1-4 depend on other teams and "
     "are the critical path — they should start well before OCI work finishes.", 7)
note(ws, r + 3,
     "The portal is already multi-cloud in design: one request form, one approval path, a "
     "'deployment target' field, and cost models for all five clouds. Azure is new BLUEPRINTS, "
     "not a new portal.", 7, LIGHT)

# ========================================================= 4. CURRENT STATE ===
ws = wb.create_sheet("Current State")
title(ws, "What the portal does today — measured, not claimed",
      "Read from the running system on 2026-08-21.", 4)
header(ws, 4, ["Area", "Capability", "State", "Evidence"], [22, 44, 14, 46])

STATE = [
    ("Intake", "Guided request form, saved drafts, AI-assisted drafting", "Live",
     "React + IBM Carbon front end on a FastAPI backend"),
    ("Validation", "Server-side validation, sizing and pricing — client holds no authority", "Live",
     "Refusals explain the reason and guide the requester"),
    ("Approval", "Jira Service Management holds the approval", "Live",
     "Live instance, project SDIMD; orchestrator re-verifies independently"),
    ("Policy", "Open Policy Agent evaluates every request", "Live", "OPA running as a service"),
    ("Provisioning", "Terraform via a durable orchestrator, per-request state", "Live",
     "Real OCI resources; mock mode is the default for demos"),
    ("Audit", "Append-only, hash-chained log of every privileged action", "Live",
     "Tamper-evident chain"),
    ("Certified OCI blueprints", "apache, java21, nodejs20, oci-oke, postgres16, python312, redis7", "Live",
     "7 certified — each earned by a real build"),
    ("Catalogue breadth", "31 OCI / 32 Azure / 32 AWS / 32 GCP / 27 on-prem technologies listed", "Partial",
     "GAP: only OCI has certified blueprints. Listing is not capability."),
    ("FinOps", "Cost estimates, rate cards and budgets for all five clouds", "Live",
     "Azure pricing already implemented"),
    ("Lifecycle", "TTL, decommission, drift detection, cloud state observe/actuate", "Live",
     "Non-prod environments expire automatically"),
    ("AI", "Recommends only — sizing, cost explanation, failure triage, drafting", "Live",
     "Never approves, never provisions, holds no credentials"),
    ("Automation posture", "Fully autonomous: approval in Jira triggers a real build", "Live",
     "Deliberate choice; nothing provisions without an approval"),
]
r = 5
for area, cap, state, ev in STATE:
    write_row(ws, r, [area, cap, state, ev], wrap_cols=(2, 4))
    c = ws.cell(row=r, column=3)
    c.fill = PatternFill("solid", fgColor=GREEN if state == "Live" else AMBER)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.font = Font(name=FONT, size=10, bold=True)
    ws.row_dimensions[r].height = 30
    r += 1

note(ws, r + 1,
     "Eight defects were found and fixed on 17-18 Aug while proving OKE and PostgreSQL. Every one "
     "was a value hard-coded in the portal that the cloud no longer accepted. This is why "
     "certification is being moved onto evidence (Phase P5).", 4)

# ==================================================== 5. RISKS & DECISIONS ===
ws = wb.create_sheet("Risks & Decisions")
title(ws, "Risks, dependencies and decisions needed", "Owner column is for you to complete.", 6)
header(ws, 4, ["Ref", "Type", "Item", "Impact", "Mitigation / action", "Owner"],
       [7, 12, 40, 12, 46, 16])

ROWS = [
    ("R1", "Risk", "Catalogue lists technologies the portal cannot build (32 Azure, 0 certified)",
     "High", "Phase P5 makes certification evidence-based; uncertified items route to manual fulfilment", ""),
    ("R2", "Risk", "Proof builds cost real money and take real time (an OKE proof is ~35 min live)",
     "Medium", "Cost envelope caps each proof; cheap workloads nightly, expensive ones weekly", ""),
    ("R3", "Risk", "Cloud providers retire versions, shapes and images without notice",
     "High", "Catalogue facts are fetched from the cloud, never hard-coded — already the rule for OCI", ""),
    ("R4", "Dependency", "Azure subscriptions, Entra service principal, and landing-zone networks",
     "High", "Owned by other teams; start P7 early — it is the critical path for all Azure work", ""),
    ("R5", "Dependency", "Jira availability — a submission fails if Jira is unreachable",
     "Medium", "Observed twice on 17 Aug. Consider retry-on-transient rather than failing the form", ""),
    ("R6", "Decision", "Oracle Database 19c: Base Database VM, Exadata, or self-installed?",
     "High", "Licensing and cost differ materially. Needed before P6 can be scoped", ""),
    ("R7", "Decision", "Sandbox tier for proof builds — currently Development",
     "Medium", "Agreed 'for the time being'. A dedicated tier removes the risk of colliding with real work", ""),
    ("R8", "Decision", "Proof cadence, certification validity period, and merge policy",
     "Medium", "Needed before Phase P5 increments C2 and C4", ""),
    ("R9", "Risk", "Single-region OCI (me-dubai-1 has one availability domain)",
     "Medium", "Constrains HA options — regional durability is unavailable. Factor into DR planning", ""),
]
r = 5
for ref, kind, item, impact, action, owner in ROWS:
    write_row(ws, r, [ref, kind, item, impact, action, owner], wrap_cols=(3, 5))
    c = ws.cell(row=r, column=4)
    c.fill = PatternFill("solid", fgColor={"High": RED, "Medium": AMBER}.get(impact, GREY))
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.font = Font(name=FONT, size=10, bold=True)
    ws.cell(row=r, column=6).fill = PatternFill("solid", fgColor="FFFF00")
    ws.row_dimensions[r].height = 32
    r += 1

note(ws, r + 1, "Yellow cells are for you to fill in.", 6, "FFFF00")

for sheet in wb.worksheets:
    sheet.sheet_view.showGridLines = False

wb.save(OUT)
print(f"written: {OUT}")
