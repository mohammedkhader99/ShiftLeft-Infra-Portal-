"""Portal help / knowledge base (F-INT-08 help layer).

Answers plain-English questions about what the portal's pages and features do —
"what does the Reduce capacity request do?", "what's the Admin page for?" — from a
curated knowledge base that is the single source of truth here. Because every
answer is grounded in this KB, the assistant can't invent a feature that doesn't
exist.

Mock-first (returns the best-matching KB entry; offline and free) with a live
Claude adapter that phrases an answer using ONLY this KB as its source and says so
when a question isn't covered. Mirrors the other AI features' mock/live pattern
(shared client via api/ai_drafter).
"""

from __future__ import annotations

import re

from api.ai_drafter import AiUnavailable, ai_mode, ai_model, anthropic_client

# Each topic: a stable key, a display title, the category it groups under, the
# trigger phrases that should match it, and the plain-language answer. Multi-word
# trigger phrases score higher than single words, so the most specific topic wins.
TOPICS: list[dict] = [
    # --- Pages ---------------------------------------------------------------
    {
        "key": "page-new-request", "title": "New Request page", "category": "Pages",
        "keywords": ["new request", "request page", "raise a request", "request form",
                     "create a request", "make a request", "submit a request"],
        "answer": (
            "The New Request page is where you ask for infrastructure. You pick a request "
            "type, a deployment target (on-prem or a cloud), the technologies and sizes you "
            "need, and governance details (project, cost centre, data classification, "
            "business justification). The portal validates everything, prices it live, and — "
            "once you submit — raises a Jira ticket for approval. Nothing is provisioned "
            "until it's approved."
        ),
    },
    {
        "key": "page-my-requests", "title": "My Requests page", "category": "Pages",
        "keywords": ["my requests", "my request", "track my", "requests page", "request status page"],
        "answer": (
            "My Requests lists the requests you've raised and their live status (draft, "
            "submitted, awaiting approval, provisioning, done, rejected, failed). From here "
            "you can track the approval SLA, download an evidence pack, renew an "
            "environment's lifetime, check for cloud drift, reconcile cloud state, stop/start "
            "an environment, transfer ownership, and — if something failed — ask the AI to "
            "diagnose it."
        ),
    },
    {
        "key": "page-overview", "title": "Overview / Estate page", "category": "Pages",
        "keywords": ["overview page", "estate", "dashboard", "overview"],
        "answer": (
            "The Overview (Estate) page is the at-a-glance dashboard of everything "
            "provisioned: total environments, the monthly run-rate, and key health "
            "indicators, plus an Anomalies panel that flags unusual spend or risky requests. "
            "It's read-only and aimed at oversight."
        ),
    },
    {
        "key": "page-showback", "title": "Showback page", "category": "Pages",
        "keywords": ["showback", "chargeback", "who is spending", "cost by", "spend by"],
        "answer": (
            "Showback shows who is spending what. It breaks the monthly cost down by cost "
            "centre, project, environment, or owner, with a Running vs Committed toggle. It "
            "also hosts the FinOps panels: Budgets, Forecast, Variance, Sustainability, "
            "Optimisation, and Scheduled shutdown."
        ),
    },
    {
        "key": "page-reports", "title": "Reports page", "category": "Pages",
        "keywords": ["reports page", "report", "reports", "download report", "csv", "pdf report",
                     "subscription"],
        "answer": (
            "The Reports page generates and views reports (spend forecast, anomalies, estate "
            "summary) on demand and lets you download them as CSV or PDF. You can also set up "
            "scheduled report subscriptions that run automatically on a cadence."
        ),
    },
    {
        "key": "page-activity", "title": "Activity page", "category": "Pages",
        "keywords": ["activity page", "activity feed", "event stream", "activity", "live feed"],
        "answer": (
            "The Activity page is a live feed of what's happening across the portal — "
            "requests submitted, approved, provisioned, environments changed — published from "
            "the tamper-evident audit log. It's read-only, useful for oversight or for "
            "feeding external systems."
        ),
    },
    {
        "key": "page-assistant", "title": "Assistant page", "category": "Pages",
        "keywords": ["assistant page", "assistant", "chatbot", "chat bot", "this page", "bot"],
        "answer": (
            "The Assistant (this page) lets you query and act on requests in plain English — "
            "list what's pending, check a request's status, or approve/reject (always with "
            "your confirmation, recorded in Jira). It can also explain what the portal's "
            "pages and features do. It interprets your words; it never decides on its own."
        ),
    },
    {
        "key": "page-admin", "title": "Admin page", "category": "Pages",
        "keywords": ["admin page", "admin", "administration", "administrator", "settings page"],
        "answer": (
            "The Admin page is for platform administrators. It covers access control (mapping "
            "identity groups to portal roles), budgets and quotas, API keys for programmatic "
            "access, the effective governance/FinOps configuration, and the list of orphaned "
            "environments."
        ),
    },

    # --- Request types -------------------------------------------------------
    {
        "key": "type-create", "title": "Create request", "category": "Request types",
        "keywords": ["create request", "create a new", "new environment", "brand new"],
        "answer": (
            "A Create request provisions a brand-new environment — you choose the "
            "technologies, sizes and deployment target, and after approval it's built and "
            "charged to your cost centre."
        ),
    },
    {
        "key": "type-clone", "title": "Clone request", "category": "Request types",
        "keywords": ["clone", "copy an environment", "duplicate", "copy of"],
        "answer": (
            "A Clone request creates a copy of an existing environment — you point at a "
            "source environment and it reproduces its stack on the chosen target. It's "
            "charged and counted like a new environment."
        ),
    },
    {
        "key": "type-sandbox", "title": "Sandbox request", "category": "Request types",
        "keywords": ["sandbox", "throwaway", "experiment", "play environment"],
        "answer": (
            "A Sandbox request spins up a short-lived, throwaway environment for "
            "experimentation. It's automatically given a short expiry (TTL) so it cleans "
            "itself up, keeping costs down."
        ),
    },
    {
        "key": "type-temporary", "title": "Temporary request", "category": "Request types",
        "keywords": ["temporary", "time-boxed", "expiry date", "temp environment", "time boxed"],
        "answer": (
            "A Temporary request provisions an environment with an explicit expiry date you "
            "set. It's like a normal environment but time-boxed — it's flagged for cleanup "
            "when it expires."
        ),
    },
    {
        "key": "type-reduce", "title": "Reduce capacity request", "category": "Request types",
        "keywords": ["reduce capacity", "reduce", "scale down", "downsize", "shrink", "smaller",
                     "lower the size", "scale back"],
        "answer": (
            "A Reduce capacity request lowers the size — and therefore the cost — of an "
            "existing environment, for example scaling a database or app tier down. You pick "
            "the environment and the smaller sizes; it goes through approval like any other "
            "change, and the new, lower cost is re-computed."
        ),
    },
    {
        "key": "type-decommission", "title": "Decommission request", "category": "Request types",
        "keywords": ["decommission", "retire", "tear down", "delete environment", "remove environment"],
        "answer": (
            "A Decommission request retires an existing environment. After approval, the "
            "orchestrator tears it down; it's recorded and audited, and the environment stops "
            "being charged."
        ),
    },
    {
        "key": "type-refresh", "title": "Refresh request", "category": "Request types",
        "keywords": ["refresh", "refresh from", "copy data", "rebuild from"],
        "answer": (
            "A Refresh request rebuilds or refreshes an environment from a source (for "
            "example copying production data into a test environment). It references the "
            "source and goes through the normal approval path."
        ),
    },
    {
        "key": "type-restore", "title": "Restore request", "category": "Request types",
        "keywords": ["restore", "restore from backup", "recover", "from a backup"],
        "answer": (
            "A Restore request recovers an environment from a backup — you reference the "
            "backup to restore from. It's governed and approved like other changes."
        ),
    },
    {
        "key": "type-dr", "title": "Disaster Recovery request", "category": "Request types",
        "keywords": ["disaster recovery", "dr request", "dr copy", "failover"],
        "answer": (
            "A Disaster Recovery request stands up a DR copy of an environment on a chosen "
            "target, referencing the primary. It follows the normal validate → approve → "
            "provision flow."
        ),
    },

    # --- Request form features ----------------------------------------------
    {
        "key": "form-targets", "title": "Deployment targets", "category": "Request form",
        "keywords": ["deployment target", "deployment targets", "on-prem", "on prem", "which cloud",
                     "azure", "oci", "aws", "gcp", "cloud choice"],
        "answer": (
            "The deployment target is where the environment runs: on-premises or one of the "
            "clouds (Azure, OCI, AWS, GCP). The technology catalogue filters to what's "
            "available on the target you pick, and pricing is computed for that target. "
            "Cloud-specific services (like AWS RDS) only appear on their own cloud."
        ),
    },
    {
        "key": "form-catalogue", "title": "Technology & size catalogue", "category": "Request form",
        "keywords": ["catalogue", "catalog", "technology", "technologies", "size", "sizes",
                     "which technologies", "stack"],
        "answer": (
            "The technology catalogue lists the approved technologies you can request, each "
            "with a size (small→xlarge). It's target-aware — only technologies offered on "
            "your chosen deployment target appear — and end-of-life technologies can't be "
            "requested."
        ),
    },
    {
        "key": "form-advanced", "title": "Advanced options", "category": "Request form",
        "keywords": ["advanced options", "high availability", "ha", "backup retention", "monitoring",
                     "support tier"],
        "answer": (
            "Advanced options add cost-affecting choices: high availability (redundant "
            "nodes), backup retention, monitoring level, and support tier. Each is re-priced "
            "server-side and shown in the live cost."
        ),
    },
    {
        "key": "form-cost", "title": "Live cost & Explain this cost", "category": "Request form",
        "keywords": ["live cost", "cost panel", "estimate", "how much", "explain this cost",
                     "explain cost", "pricing", "price"],
        "answer": (
            "The live cost panel shows the authoritative monthly and one-time estimate for "
            "your request as you build it, computed server-side. 'Explain this cost' has the "
            "AI narrate what's driving the price and suggest optimisations — it only "
            "explains, it never changes the request."
        ),
    },
    {
        "key": "form-draft", "title": "Draft with AI", "category": "Request form",
        "keywords": ["draft with ai", "draft ai", "describe what i need", "fill in the form", "auto fill"],
        "answer": (
            "'Draft with AI' turns a plain-English description into a filled-in request "
            "draft, constrained to the approved catalogue. It only suggests a draft for you "
            "to review, edit and submit — it never submits, prices or provisions."
        ),
    },
    {
        "key": "form-recommend", "title": "Recommend cloud & size", "category": "Request form",
        "keywords": ["recommend cloud", "recommend size", "which cloud is cheapest", "best value cloud",
                     "compare clouds", "cross-cloud"],
        "answer": (
            "'Recommend cloud & size' takes a workload description and recommends a stack, "
            "then prices it across every cloud it can run on so you can compare and pick the "
            "best value. The AI picks the stack; the portal computes the prices."
        ),
    },
    {
        "key": "form-classification", "title": "Data classification", "category": "Request form",
        "keywords": ["data classification", "classification", "confidential", "restricted", "sensitivity",
                     "public internal"],
        "answer": (
            "Data classification (public, internal, confidential, restricted) records how "
            "sensitive the data is. It drives policy — for example restricted data can be "
            "blocked from public cloud exposure — and appears on the request and evidence "
            "pack."
        ),
    },
    {
        "key": "form-summary", "title": "Environment & Approval summary panels", "category": "Request form",
        "keywords": ["summary panel", "approval summary", "environment summary", "who approves",
                     "before you submit"],
        "answer": (
            "The Environment and Approval summary panels on the form preview what you're "
            "about to request and who needs to approve it — plus any change window or SLA "
            "that will apply — before you submit."
        ),
    },

    # --- FinOps (Showback) panels -------------------------------------------
    {
        "key": "fin-budgets", "title": "Budgets", "category": "FinOps (Showback)",
        "keywords": ["budget", "budgets", "spending limit", "spend ceiling", "over budget"],
        "answer": (
            "Budgets set a monthly spend ceiling per cost centre. When a new request would "
            "push projected committed spend near or over the budget, the portal warns (or "
            "blocks, if enforcement is on). You manage budgets on the Showback page."
        ),
    },
    {
        "key": "fin-forecast", "title": "Forecast", "category": "FinOps (Showback)",
        "keywords": ["forecast", "projected spend", "future spend", "spend projection"],
        "answer": (
            "The Forecast panel projects monthly spend over coming months: today's run-rate, "
            "plus in-flight requests as they provision, minus non-prod environments as their "
            "lifetimes expire."
        ),
    },
    {
        "key": "fin-variance", "title": "Variance (actual vs estimate)", "category": "FinOps (Showback)",
        "keywords": ["variance", "actual vs estimate", "actual cost", "billed vs", "cost drift"],
        "answer": (
            "Variance compares the actual billed cost of an environment against its approved "
            "estimate and flags significant drift, so you can catch surprises between what "
            "was quoted and what's really being charged."
        ),
    },
    {
        "key": "fin-sustainability", "title": "Sustainability", "category": "FinOps (Showback)",
        "keywords": ["sustainability", "carbon", "energy", "co2", "green", "footprint"],
        "answer": (
            "The Sustainability panel gives an indicative estimate of each environment's "
            "energy use and carbon footprint, from its size and documented power/grid "
            "factors, with an estate total. It's indicative, not metered."
        ),
    },
    {
        "key": "fin-optimisation", "title": "Optimisation digest", "category": "FinOps (Showback)",
        "keywords": ["optimisation", "optimization", "savings", "save money", "rightsize", "right-size",
                     "cost saving"],
        "answer": (
            "The Optimisation digest lists money-saving opportunities per owner — rightsizing "
            "oversized components, turning off HA, downgrading over-spec monitoring/support — "
            "each with a saving re-priced through the cost engine. It recommends only."
        ),
    },
    {
        "key": "fin-shutdown", "title": "Scheduled shutdown", "category": "FinOps (Showback)",
        "keywords": ["scheduled shutdown", "auto shutdown", "shut down", "off hours", "pause non-prod",
                     "business hours"],
        "answer": (
            "Scheduled shutdown estimates the saving from pausing non-prod environments "
            "outside business hours (production and DR never shut down). It quantifies the "
            "monthly saving; actually pausing is opt-in."
        ),
    },

    # --- My Requests actions -------------------------------------------------
    {
        "key": "act-sla", "title": "Approval SLA", "category": "My Requests actions",
        "keywords": ["approval sla", "sla", "on time", "due soon", "breached", "how long to approve"],
        "answer": (
            "Each awaiting-approval request has an SLA — the portal shows whether approval is "
            "on-time, due soon, or breached, and escalates a breach in Jira."
        ),
    },
    {
        "key": "act-evidence", "title": "Evidence pack", "category": "My Requests actions",
        "keywords": ["evidence pack", "evidence", "audit pdf", "compliance pack", "proof"],
        "answer": (
            "The evidence pack is a downloadable PDF for a request — the full record of the "
            "request, cost, approval and audit trail, with a re-verified tamper-evidence "
            "attestation. It's for audits and governance."
        ),
    },
    {
        "key": "act-renew", "title": "Renew (extend TTL)", "category": "My Requests actions",
        "keywords": ["renew", "extend", "extend lifetime", "keep the environment", "ttl renew"],
        "answer": (
            "Renew extends a non-prod environment's lifetime (TTL) before it expires, so it "
            "isn't flagged for cleanup."
        ),
    },
    {
        "key": "act-reconcile", "title": "Reconcile cloud state", "category": "My Requests actions",
        "keywords": ["reconcile", "cloud state", "out of band", "actual state", "reconcile cloud"],
        "answer": (
            "Reconcile cloud state asks the orchestrator for the real state of a request's "
            "cloud resources and compares it to the portal's registry, flagging anything "
            "changed out-of-band. It observes only — it never changes cloud state."
        ),
    },
    {
        "key": "act-drift", "title": "Drift check", "category": "My Requests actions",
        "keywords": ["drift check", "drift", "check drift", "infrastructure drift", "terraform drift"],
        "answer": (
            "Drift check re-plans a provisioned environment's infrastructure-as-code "
            "read-only and reports whether the real infrastructure has drifted from what was "
            "declared."
        ),
    },
    {
        "key": "act-actuate", "title": "Stop / Start (actuate)", "category": "My Requests actions",
        "keywords": ["stop start", "stop/start", "power off", "power on", "turn off environment",
                     "stop an environment", "actuate"],
        "answer": (
            "Stop/Start lets a platform admin power a provisioned environment's compute off or "
            "on from the portal, through the orchestrator. It's a reversible operational "
            "action; production is treated carefully."
        ),
    },
    {
        "key": "act-transfer", "title": "Transfer owner", "category": "My Requests actions",
        "keywords": ["transfer owner", "reassign owner", "change owner", "ownership transfer"],
        "answer": (
            "Transfer owner reassigns an environment's owners (application/business/technical/"
            "environment). If an owner has left, the environment is flagged as orphaned until "
            "reassigned."
        ),
    },
    {
        "key": "act-health", "title": "Environment health score", "category": "My Requests actions",
        "keywords": ["health score", "health", "grade", "score", "how healthy"],
        "answer": (
            "The health score rates each environment 0–100 (grade A–E) from signals like TTL, "
            "orphan status, security-scan findings, cost variance, backup and monitoring, and "
            "lists what's pulling the score down."
        ),
    },
    {
        "key": "act-triage", "title": "Diagnose failure (AI triage)", "category": "My Requests actions",
        "keywords": ["diagnose", "triage", "why did it fail", "failure", "failed request", "what went wrong"],
        "answer": (
            "For a failed or blocked request, 'Diagnose failure' has the AI read the real "
            "failure signals and explain, in plain language, the likely cause and next steps. "
            "It diagnoses only — it never retries or changes anything."
        ),
    },

    # --- Admin sub-features ---------------------------------------------------
    {
        "key": "admin-rbac", "title": "Access control (RBAC)", "category": "Admin",
        "keywords": ["access control", "rbac", "roles", "group to role", "group mapping", "permissions",
                     "who can do what"],
        "answer": (
            "Access control maps your identity-provider groups to portal roles (requester, "
            "approver, platform admin, read-only), which decides what each person can do. "
            "It's the source of who-can-do-what when live role enforcement is on."
        ),
    },
    {
        "key": "admin-users", "title": "Users & Roles view", "category": "Admin",
        "keywords": ["users and roles", "users & roles", "who has which role", "list of users",
                     "user roles"],
        "answer": (
            "The Users & Roles view is a read-only list of who has which portal role, "
            "resolved from the group→role mapping."
        ),
    },
    {
        "key": "admin-quotas", "title": "Quotas", "category": "Admin",
        "keywords": ["quota", "quotas", "environment limit", "how many environments", "project limit"],
        "answer": (
            "Quotas cap how many environments a project may have. A new create request that "
            "would exceed the project's quota is warned (or blocked, if enforcement is on). "
            "Managed on the Admin page."
        ),
    },
    {
        "key": "admin-apikeys", "title": "API keys", "category": "Admin",
        "keywords": ["api key", "api keys", "programmatic access", "token", "integration key"],
        "answer": (
            "API keys give programmatic access to the portal API. A key acts as the user who "
            "issued it, so it inherits their roles and can never exceed them; the secret is "
            "shown once. Used by scripts and the CLI."
        ),
    },
    {
        "key": "admin-config", "title": "Configuration / posture", "category": "Admin",
        "keywords": ["configuration", "config", "posture", "governance settings", "enforcement switches",
                     "which policies are on"],
        "answer": (
            "The configuration view shows the effective governance and FinOps posture — which "
            "policies, SLAs, budgets and enforcement switches are on — at a glance, for "
            "platform admins."
        ),
    },
    {
        "key": "admin-orphans", "title": "Orphaned environments", "category": "Admin",
        "keywords": ["orphan", "orphaned", "owner left", "no owner", "departed owner"],
        "answer": (
            "Orphaned environments are provisioned environments whose owner has left the "
            "organisation. The portal detects and lists them so ownership can be transferred."
        ),
    },

    # --- How approval & governance works ------------------------------------
    {
        "key": "gov-approval", "title": "How approval works", "category": "How it works",
        "keywords": ["how does approval", "approval work", "who approves", "approval flow",
                     "how approvals", "jira approval"],
        "answer": (
            "Approvals are held in Jira, not the portal. When you submit, the portal raises a "
            "Jira ticket with the config and cost; an approver approves it there (or via the "
            "Assistant, which records the decision in Jira). Only an approved, re-verified "
            "request is provisioned — the portal and the AI never approve on their own."
        ),
    },
    {
        "key": "gov-sod", "title": "Segregation of duties", "category": "How it works",
        "keywords": ["segregation of duties", "four eyes", "four-eyes", "approve my own", "self approval",
                     "sod"],
        "answer": (
            "Segregation of duties means you can't approve or act on a request you raised — a "
            "different person must approve it. It's enforced everywhere approvals happen."
        ),
    },
    {
        "key": "gov-audit", "title": "Audit trail", "category": "How it works",
        "keywords": ["audit", "audit trail", "audit log", "tamper", "history of changes", "who did what"],
        "answer": (
            "Every privileged action is written to an append-only, tamper-evident audit log "
            "(hash-chained), so the full history can be verified and shown in evidence packs."
        ),
    },
    {
        "key": "gov-change-window", "title": "Change windows", "category": "How it works",
        "keywords": ["change window", "change windows", "when does it provision", "provisioning window",
                     "maintenance window"],
        "answer": (
            "Change windows restrict when approved requests actually provision — e.g. only "
            "during agreed days and hours. A request approved outside the window waits and "
            "auto-provisions when the window opens."
        ),
    },
    {
        "key": "gov-waiver", "title": "Policy waivers", "category": "How it works",
        "keywords": ["waiver", "waivers", "policy exception", "exception", "override policy"],
        "answer": (
            "A policy waiver is a documented, expiring exception an authorised approver "
            "(never the requester) grants so a policy-blocked request can proceed. It's "
            "audited and shown in the evidence pack."
        ),
    },
    {
        "key": "gov-quorum", "title": "Approval quorum", "category": "How it works",
        "keywords": ["quorum", "how many approvers", "multiple approvers", "number of approvers"],
        "answer": (
            "Approval quorum requires a set number of distinct approvers (never the "
            "requester) before an approved request is provisioned."
        ),
    },
    {
        "key": "gov-ttl", "title": "Environment lifetime (TTL)", "category": "How it works",
        "keywords": ["ttl", "lifetime", "expiry", "how long does an environment", "environment expire"],
        "answer": (
            "Non-production environments are given a lifetime (TTL); owners are warned before "
            "expiry and can renew. Production and DR are exempt. It keeps unused environments "
            "from lingering and costing money."
        ),
    },
    {
        "key": "gov-cli", "title": "Command-line tool (infractl)", "category": "How it works",
        "keywords": ["cli", "command line", "infractl", "terminal", "scriptable"],
        "answer": (
            "The portal has a command-line tool (infractl) that authenticates with an API "
            "key and can list requests, check status, renew, and more — the same actions, "
            "scriptable."
        ),
    },
    {
        "key": "gov-ai", "title": "The AI helpers", "category": "How it works",
        "keywords": ["ai helpers", "ai features", "does the ai", "can the ai", "artificial intelligence",
                     "what can the ai"],
        "answer": (
            "The portal's AI helpers all recommend or explain only — Draft with AI, Recommend "
            "cloud & size, Explain this cost, Diagnose failure, and this Assistant. They never "
            "approve, price authoritatively, or provision; a human and the normal Jira flow "
            "stay in control."
        ),
    },
]


# --- Matching + answering -----------------------------------------------------

def _kw_matches(keyword: str, text: str) -> bool:
    """Whole-word/phrase match, so a short trigger like 'ha' matches the standalone
    word — not the 'ha' inside 'w-ha-t'."""
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def find_topic(message: str) -> dict | None:
    """The best-matching KB topic for a message, or None. A trigger phrase scores
    by its word count, so a specific multi-word match (e.g. 'reduce capacity')
    beats an incidental single-word one."""
    text = (message or "").lower()
    best: dict | None = None
    best_score = 0
    for topic in TOPICS:
        score = sum(len(kw.split()) for kw in topic["keywords"] if _kw_matches(kw, text))
        if score > best_score:
            best, best_score = topic, score
    return best if best_score >= 1 else None


def overview() -> str:
    """The catalogue of things the assistant can explain, grouped by area — shown
    when a question doesn't match a specific topic."""
    lines = ["I can explain these parts of the portal — just ask about any of them:"]
    seen: list[str] = []
    for topic in TOPICS:
        if topic["category"] not in seen:
            seen.append(topic["category"])
            lines.append(f"\n{topic['category']}:")
        lines.append(f"  • {topic['title']}")
    return "\n".join(lines)


_SYSTEM = (
    "You are the in-portal help assistant for an infrastructure provisioning "
    "portal. Answer the user's question about the portal's pages and features "
    "using ONLY the provided documentation. Be concise (2–4 sentences), plain, and "
    "practical. If the documentation doesn't cover the question, say you don't have "
    "information on that and suggest they ask about one of the documented features. "
    "Never invent features, prices, or behaviour."
)


def _kb_text() -> str:
    return "\n\n".join(f"## {t['title']}\n{t['answer']}" for t in TOPICS)


def _answer_live(message: str) -> dict:
    client = anthropic_client()
    try:
        resp = client.messages.create(
            model=ai_model(),
            max_tokens=600,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Portal documentation:\n{_kb_text()}\n\nQuestion: {message.strip()}",
            }],
        )
    except Exception as exc:  # noqa: BLE001 — surface any SDK/transport/API error cleanly
        raise AiUnavailable(f"The AI service call failed: {exc}") from exc
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if not text:
        raise AiUnavailable("The AI service returned an empty answer.")
    return {"mode": "live", "matched": True, "title": None, "response": text.strip()}


def answer(message: str) -> dict:
    """Answer a portal question. Returns {mode, matched, title, response}. Mock
    returns the matching KB entry (or the topic overview); live has Claude phrase
    an answer grounded ONLY in the KB."""
    if ai_mode() == "live":
        return _answer_live(message)
    topic = find_topic(message)
    if topic is None:
        return {"mode": "mock", "matched": False, "title": None, "response": overview()}
    return {"mode": "mock", "matched": True, "title": topic["title"],
            "response": f"{topic['title']}\n\n{topic['answer']}"}
