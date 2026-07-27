"""Mock Terraform-style plan preview (increment 1.8, F-ORC-02).

Builds a human-readable change preview from the request's resolved sizing, to
attach to the Jira ticket so the approver sees what will be provisioned. This is
a mock preview — the real orchestrator produces the authoritative plan later.
"""

from sqlalchemy.orm import Session

from api.sizing import resolve_components
from db.models import Request

_VERB = {
    "create": "add",
    "add": "add",
    "resize": "change",
    "decommission": "destroy",
}


def build_plan_preview(req: Request, session: Session) -> str:
    components = [
        {"technology_code": c.technology_code, "size": c.size} for c in req.components
    ]
    sizing = resolve_components(components, session)
    verb = _VERB.get(req.request_type or "", "add")
    env = req.environment_name or req.target_environment or "environment"
    target = req.deployment_target or "on-prem"

    lines = [f"Terraform plan (preview) — {env} @ {target}"]
    symbol = {"add": "+", "change": "~", "destroy": "-"}.get(verb, "+")
    count = 0
    for row in sizing["components"]:
        if row["resolved"]:
            count += 1
            lines.append(
                f"  {symbol} module.{env}.{row['technology_code']}"
                f"    {row['vcpu']} vCPU / {row['memory_gb']} GB / {row['storage_gb']} GB"
            )
    add = count if verb == "add" else 0
    change = count if verb == "change" else 0
    destroy = count if verb == "destroy" else 0
    lines.append(f"Plan: {add} to add, {change} to change, {destroy} to destroy.")
    return "\n".join(lines)
