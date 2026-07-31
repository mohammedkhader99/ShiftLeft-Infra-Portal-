"""Sustainability estimate (F-FIN-13).

An **indicative** energy (kWh/month) and carbon (kgCO2e/month) figure per
provisioned environment, derived from its sizing (vCPU / RAM / storage) times
transparent, documented coefficients — power draw per unit, data-centre PUE
overhead, and grid carbon intensity (cloud lower than on-prem). These are
mock/demo constants (configurable via SUSTAIN_* env vars), swappable for the
customer's real figures.

It is a directional signal for green-IT reporting, **not a metered value**, and
it is read-only — it changes nothing.
"""

import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.sizing import resolve_components
from db.models import Request

HOURS_PER_MONTH = 730

# Default coefficients (indicative). Per-target defaults reflect that public
# cloud data centres are typically more efficient (lower PUE) than on-prem.
_PUE_DEFAULTS = {"onprem": 1.6, "azure": 1.18, "oci": 1.15}
_CARBON_DEFAULTS = {"onprem": 500.0, "azure": 380.0, "oci": 380.0}  # gCO2e/kWh
_KG_PER_CAR_KM = 0.17          # ~kgCO2e per km driven
_KG_ABSORBED_PER_TREE_YEAR = 21.0


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _pue(target: str | None) -> float:
    key = (target or "").strip().lower()
    return _f(f"SUSTAIN_PUE_{key.upper()}", _PUE_DEFAULTS.get(key, 1.5))


def _carbon_intensity(target: str | None) -> float:
    key = (target or "").strip().lower()
    return _f(f"SUSTAIN_CARBON_{key.upper()}", _CARBON_DEFAULTS.get(key, 450.0))


def footprint(vcpu: float, memory_gb: float, storage_gb: float, target: str | None) -> dict:
    """Indicative monthly energy + carbon for a given sizing on a target."""
    watts = (
        vcpu * _f("SUSTAIN_W_PER_VCPU", 12.0)
        + memory_gb * _f("SUSTAIN_W_PER_GB_RAM", 0.4)
        + storage_gb * _f("SUSTAIN_W_PER_GB_STORAGE", 0.005)
    )
    pue = _pue(target)
    kwh = watts / 1000.0 * HOURS_PER_MONTH * pue
    carbon_kg = kwh * _carbon_intensity(target) / 1000.0
    return {
        "energy_kwh_month": round(kwh, 1),
        "carbon_kg_month": round(carbon_kg, 1),
        "pue": pue,
        "carbon_intensity": _carbon_intensity(target),
    }


def _equivalents(carbon_kg_month: float) -> dict:
    """Relatable comparisons for a monthly carbon figure."""
    return {
        "car_km": round(carbon_kg_month / _KG_PER_CAR_KM),
        "trees_year": round(carbon_kg_month * 12 / _KG_ABSORBED_PER_TREE_YEAR, 1),
    }


def estate_footprint(session: Session) -> dict:
    """Indicative footprint per provisioned environment + the estate total."""
    environments: list[dict] = []
    total_kwh = 0.0
    total_carbon = 0.0
    for req in session.scalars(select(Request).where(Request.status == "provisioned")):
        components = [{"technology_code": c.technology_code, "size": c.size} for c in req.components]
        totals = resolve_components(components, session)["totals"]
        fp = footprint(totals["vcpu"], totals["memory_gb"], totals["storage_gb"], req.deployment_target)
        total_kwh += fp["energy_kwh_month"]
        total_carbon += fp["carbon_kg_month"]
        environments.append({
            "reference": req.reference,
            "environment": req.environment_name or req.target_environment,
            "deployment_target": req.deployment_target,
            "vcpu": totals["vcpu"],
            "memory_gb": totals["memory_gb"],
            "storage_gb": totals["storage_gb"],
            "energy_kwh_month": fp["energy_kwh_month"],
            "carbon_kg_month": fp["carbon_kg_month"],
        })
    environments.sort(key=lambda e: e["carbon_kg_month"], reverse=True)
    total_carbon = round(total_carbon, 1)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_energy_kwh_month": round(total_kwh, 1),
        "total_carbon_kg_month": total_carbon,
        "equivalents": _equivalents(total_carbon),
        "environment_count": len(environments),
        "environments": environments,
        "note": "Indicative estimate from sizing x documented power/PUE/grid coefficients — not metered.",
    }
