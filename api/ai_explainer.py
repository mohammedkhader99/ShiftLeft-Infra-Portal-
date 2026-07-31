"""AI cost explanation (F-RPT-07).

Turns a request's *authoritative* server-computed cost breakdown into a plain-
English explanation of what drives the bill, plus a few advisory tips to reduce
it.

Authority boundary (ARCHITECTURE.md §7, hard rule): the AI **explains and
suggests only**. It never changes the request, re-prices, submits, or provisions.
The numbers come from `estimate_cost` (P2 — pricing is authoritative server-side)
and the cost-driver ranking is computed here deterministically; the AI (live
mode) only writes the prose around those fixed numbers.

Two modes (AI_MODE), reusing the F-RPT-06 pattern (see [[ai-features]]):
  - mock  (default): a deterministic explanation built from the breakdown. No
           network, no key — demos and tests offline.
  - live:  the Claude API writes a richer narrative from the same numbers.
"""

import json

from sqlalchemy.orm import Session

from api.ai_drafter import AiUnavailable, ai_mode, ai_model, anthropic_client
from api.pricing import TECHNOLOGY_LICENCE, estimate_cost

# Category key -> the label shown to the user.
_CATEGORY_LABELS = {
    "compute": "Compute",
    "storage": "Storage",
    "licence": "Licence",
    "backup": "Backup",
    "monitoring": "Monitoring",
    "support": "Support",
}


def _drivers(by_category: dict, monthly: float) -> list[dict]:
    """Rank the non-zero cost categories, biggest first, with each one's share."""
    rows = []
    for key, label in _CATEGORY_LABELS.items():
        amount = round(float(by_category.get(key, 0) or 0), 2)
        if amount > 0:
            pct = round(amount / monthly * 100, 1) if monthly > 0 else 0.0
            rows.append({"category": key, "label": label, "amount": amount, "pct": pct})
    rows.sort(key=lambda r: r["amount"], reverse=True)
    return rows


def _large_components(components: list[dict]) -> list[str]:
    return [
        (c.get("technology_code") or "?")
        for c in components
        if (c.get("size") or "").strip().lower() in ("large", "xlarge")
    ]


def _licenced_components(components: list[dict]) -> list[str]:
    return [
        (c.get("technology_code") or "?")
        for c in components
        if (c.get("technology_code") or "") in TECHNOLOGY_LICENCE
    ]


def _mock_tips(breakdown: dict, drivers: dict, components: list[dict], advanced: dict) -> list[str]:
    """Deterministic, number-specific advisory tips. Advisory only — the requester
    decides; nothing here changes the request."""
    cur = breakdown["currency"]
    cats = {d["category"]: d for d in drivers}
    tips: list[str] = []

    if advanced.get("high_availability") and "compute" in cats:
        tips.append(
            f"High availability is on, which doubles compute to {cats['compute']['amount']:.0f} "
            f"{cur}/mo. Turn it off for non-critical environments to roughly halve that."
        )
    larges = _large_components(components)
    if larges and ("compute" in cats or "storage" in cats):
        which = ", ".join(sorted(set(larges)))
        tips.append(
            f"{which} {'is' if len(set(larges)) == 1 else 'are'} sized large/xlarge — the main "
            "driver of compute and storage. If that capacity isn't required, a smaller size cuts both."
        )
    if str(advanced.get("backup_retention")) == "90" and "backup" in cats:
        tips.append(
            f"Backup retention is 90 days (adds {cats['backup']['amount']:.0f} {cur}/mo). "
            "30 days would roughly halve the backup line."
        )
    if advanced.get("monitoring_level") == "enhanced" and "monitoring" in cats:
        tips.append(
            f"Enhanced monitoring adds {cats['monitoring']['amount']:.0f} {cur}/mo; "
            "'basic' is cheaper if you don't need the extra signals."
        )
    if advanced.get("support_tier") == "premium" and "support" in cats:
        tips.append(
            f"Premium support adds {cats['support']['amount']:.0f} {cur}/mo (20% of infra); "
            "'business' is 10%."
        )
    licenced = _licenced_components(components)
    if "licence" in cats and licenced:
        which = ", ".join(sorted(set(licenced)))
        tips.append(
            f"Software licences account for {cats['licence']['amount']:.0f} {cur}/mo, from {which}. "
            "Open-source equivalents (e.g. PostgreSQL) avoid licence fees."
        )
    return tips[:4]


def _mock_summary(breakdown: dict, drivers: list[dict]) -> str:
    cur = breakdown["currency"]
    totals = breakdown["totals"]
    monthly = float(totals["monthly"])
    if not breakdown.get("known_target"):
        return "No cost yet — choose a deployment target (on-prem, Azure or OCI) to price this."
    if monthly <= 0 or not drivers:
        return "No cost yet — add a component with a size to see the estimate."
    target = breakdown.get("deployment_target") or "the chosen target"
    top = drivers[0]
    sentence = (
        f"This environment is estimated at {monthly:.0f} {cur}/month "
        f"({float(totals['annual']):.0f} {cur}/year) on {target}. "
        f"The largest driver is {top['label']} at {top['pct']:.0f}% "
        f"({top['amount']:.0f} {cur}/mo)"
    )
    if len(drivers) > 1:
        second = drivers[1]
        sentence += f", followed by {second['label']} ({second['pct']:.0f}%)"
    return sentence + "."


def _explain_mock(breakdown: dict, drivers: list[dict], payload: dict):
    components = payload.get("components") or []
    advanced = payload.get("advanced_options") or {}
    return _mock_summary(breakdown, drivers), _mock_tips(breakdown, drivers, components, advanced)


_SYSTEM = (
    "You explain the cost of an infrastructure environment to the person requesting "
    "it. You are given an authoritative, already-computed cost breakdown — do not "
    "recompute or change any numbers; quote the ones provided. Write a short, plain-"
    "English summary of what drives the monthly cost, then a few advisory tips to "
    "reduce it, each tied to a specific number or option from the breakdown. You are "
    "only explaining and suggesting — you never change the request, re-price, submit, "
    "or provision anything."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "tips"],
    "properties": {
        "summary": {"type": "string"},
        "tips": {"type": "array", "items": {"type": "string"}},
    },
}


def _explain_live(breakdown: dict, drivers: list[dict], payload: dict):
    client = anthropic_client()
    context = json.dumps({
        "currency": breakdown["currency"],
        "deployment_target": breakdown["deployment_target"],
        "totals": breakdown["totals"],
        "by_category": breakdown["by_category"],
        "ranked_drivers": drivers,
        "components": payload.get("components"),
        "advanced_options": payload.get("advanced_options") or {},
    }, indent=2)
    try:
        resp = client.messages.create(
            model=ai_model(),
            max_tokens=1500,
            system=_SYSTEM,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
            messages=[{
                "role": "user",
                "content": f"Cost breakdown:\n{context}\n\nExplain what drives this cost and how to reduce it.",
            }],
        )
    except Exception as exc:  # noqa: BLE001
        raise AiUnavailable(f"The AI service call failed: {exc}") from exc

    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if not text:
        raise AiUnavailable("The AI service returned an empty explanation.")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AiUnavailable("The AI service returned an unreadable explanation.") from exc
    tips = [t for t in (parsed.get("tips") or []) if isinstance(t, str)]
    return (parsed.get("summary") or "").strip(), tips


def explain_cost(payload: dict, session: Session) -> dict:
    """Explain a request's cost in plain English.

    `payload` = {deployment_target, components, advanced_options} (same shape as
    /api/cost). Prices it with the authoritative `estimate_cost`, ranks the cost
    drivers deterministically, and returns {mode, currency, monthly, known_target,
    drivers, summary, tips}. Nothing is saved or provisioned — it only explains.
    """
    components = payload.get("components") or []
    breakdown = estimate_cost(
        components, payload.get("deployment_target"), session, payload.get("advanced_options")
    )
    monthly = float(breakdown["totals"]["monthly"])
    drivers = _drivers(breakdown["by_category"], monthly)

    # Only call Claude when there's an actual cost to explain; otherwise the
    # deterministic path returns a trivial "no cost yet" message.
    if ai_mode() == "live" and breakdown.get("known_target") and monthly > 0:
        summary, tips = _explain_live(breakdown, drivers, payload)
        mode = "live"
    else:
        summary, tips = _explain_mock(breakdown, drivers, payload)
        mode = "mock"

    return {
        "mode": mode,
        "currency": breakdown["currency"],
        "monthly": round(monthly, 2),
        "known_target": bool(breakdown.get("known_target")),
        "drivers": drivers,
        "summary": summary,
        "tips": tips,
    }
