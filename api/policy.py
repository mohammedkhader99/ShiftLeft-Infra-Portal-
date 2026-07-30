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
