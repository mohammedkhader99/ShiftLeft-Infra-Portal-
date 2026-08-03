"""Policy gate client (increment 1.7, F-GOV-03).

The API asks OPA to evaluate the request against the Rego policy at submit time.
OPA is the policy authority (ARCHITECTURE.md §3/§9); the app only asks and obeys.
Fail-safe: if OPA can't be reached, the request is blocked, not allowed.
"""

import os

import httpx

OPA_URL = os.getenv("OPA_URL", "http://localhost:8181")
POLICY_PATH = "/v1/data/infra/authz"


class PolicyUnavailable(RuntimeError):
    """Raised when the policy engine can't be reached or errored."""


def evaluate_policy(input_doc: dict) -> dict:
    """Return {'allow': bool, 'violations': [...], 'warnings': [...]} from OPA.

    Only non-None fields are sent, so a missing tag is 'undefined' to the policy.
    `violations` block submission (F-GOV-03); `warnings` are advisory best-practice
    guidance that never blocks.
    """
    payload = {"input": {k: v for k, v in input_doc.items() if v is not None}}
    try:
        response = httpx.post(f"{OPA_URL}{POLICY_PATH}", json=payload, timeout=5.0)
        response.raise_for_status()
        result = response.json().get("result", {})
    except Exception as exc:  # noqa: BLE001 — any failure is a hard block
        raise PolicyUnavailable(str(exc)) from exc
    return {
        "allow": bool(result.get("allow")),
        "violations": sorted(result.get("violations", [])),
        "warnings": sorted(result.get("warnings", [])),
    }


def get_policy_evaluator():
    """FastAPI dependency so tests can override the evaluator without OPA."""
    return evaluate_policy


def describe_policies() -> dict:
    """The governance rules OPA currently has LOADED, from its own API (F-GOV-03).

    Descriptions come from each rule's OPA `# METADATA` annotation, so they live
    beside the rule they describe and cannot drift from it — and because this
    reads the RUNNING policy rather than a catalogue in the portal, the page shows
    what is actually enforcing, not what someone believes is deployed.

    Raises PolicyUnavailable if OPA can't be reached: a governance view that
    guesses is worse than none.
    """
    try:
        response = httpx.get(f"{OPA_URL}/v1/policies", timeout=5.0)
        response.raise_for_status()
        modules = (response.json() or {}).get("result", [])
    except Exception as exc:  # noqa: BLE001 — surface as unavailable, never as "no rules"
        raise PolicyUnavailable(str(exc)) from exc

    overview, rules = None, []
    for module in modules:
        module_id = module.get("id", "")
        if "_test" in module_id:  # the policy's own unit tests aren't governance rules
            continue
        for ann in (module.get("ast") or {}).get("annotations") or []:
            custom = ann.get("custom") or {}
            entry = {
                "title": ann.get("title") or "",
                "description": ann.get("description") or "",
                "feature": custom.get("feature") or "",
                "effect": custom.get("effect") or "",
                "applies_to": custom.get("applies_to") or "",
                "module": module_id,
            }
            if ann.get("scope") == "package":
                overview = entry
            elif entry["title"]:
                rules.append(entry)

    # Blocking rules first — they are what stops a request.
    rules.sort(key=lambda r: (r["effect"] != "block", r["title"]))
    return {
        "overview": overview,
        "rules": rules,
        "blocking": sum(1 for r in rules if r["effect"] == "block"),
        "advisory": sum(1 for r in rules if r["effect"] == "advise"),
        "modules": [m.get("id") for m in modules if "_test" not in m.get("id", "")],
    }
