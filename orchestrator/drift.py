"""Drift detection (F-LCM-09).

Given a `terraform show -json` plan for an already-applied environment, report
whether the real infrastructure has drifted from the approved/applied state: any
planned resource change that isn't a no-op (or a data read) means the world no
longer matches the recorded state. Dependency-free and pure, so it's unit-tested
without invoking terraform.
"""

# Actions that mean "in sync" — nothing would change.
IN_SYNC = (["no-op"], ["read"])


def detect_drift(plan_json: dict) -> dict:
    """Return {drift, changes, count} from a plan document. `changes` lists each
    resource whose planned actions are not a no-op/read."""
    changes = []
    for rc in plan_json.get("resource_changes", []):
        actions = (rc.get("change") or {}).get("actions", [])
        if actions and actions not in IN_SYNC:
            changes.append({"address": rc.get("address") or rc.get("type"),
                            "actions": actions})
    return {"drift": len(changes) > 0, "changes": changes, "count": len(changes)}
