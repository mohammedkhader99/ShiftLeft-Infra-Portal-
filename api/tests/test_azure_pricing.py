"""Increment 2.1 checks: the Azure pricing adapter parses, caches, and fails safe.

The real Azure API is never called here — httpx is mocked.
"""

import pytest

from api.adapters import azure_pricing
from api.adapters.azure_pricing import AzureUnavailable


@pytest.fixture(autouse=True)
def clear_cache():
    azure_pricing._cache.clear()
    yield
    azure_pricing._cache.clear()


class _Resp:
    def __init__(self, items):
        self._items = items

    def raise_for_status(self):
        return None

    def json(self):
        return {"Items": self._items}


# A realistic Retail Prices response: Windows, Spot and Low Priority rows must be
# ignored; the plain on-demand Linux hourly row is the one we want.
ITEMS = [
    {"armSkuName": "Standard_D4s_v5", "unitPrice": 0.86, "currencyCode": "AED",
     "unitOfMeasure": "1 Hour", "meterName": "D4s v5", "productName": "Dsv5 Series Windows"},
    {"armSkuName": "Standard_D4s_v5", "unitPrice": 0.20, "currencyCode": "AED",
     "unitOfMeasure": "1 Hour", "meterName": "D4s v5 Spot", "productName": "Dsv5 Series"},
    {"armSkuName": "Standard_D4s_v5", "unitPrice": 0.4715, "currencyCode": "AED",
     "unitOfMeasure": "1 Hour", "meterName": "D4s v5", "productName": "Dsv5 Series"},
]


def test_vm_hourly_picks_on_demand_linux(monkeypatch):
    monkeypatch.setattr(azure_pricing.httpx, "get", lambda *a, **k: _Resp(ITEMS))
    assert azure_pricing.vm_hourly("medium") == 0.4715


def test_vm_monthly_multiplies_by_hours(monkeypatch):
    monkeypatch.setattr(azure_pricing.httpx, "get", lambda *a, **k: _Resp(ITEMS))
    assert azure_pricing.vm_monthly("medium") == pytest.approx(0.4715 * 730)


def test_result_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp(ITEMS)

    monkeypatch.setattr(azure_pricing.httpx, "get", fake_get)
    azure_pricing.vm_hourly("medium")
    azure_pricing.vm_hourly("medium")
    assert calls["n"] == 1  # second call served from cache


def test_no_match_raises(monkeypatch):
    monkeypatch.setattr(azure_pricing.httpx, "get", lambda *a, **k: _Resp([]))
    with pytest.raises(AzureUnavailable):
        azure_pricing.vm_hourly("small")


def test_http_error_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(azure_pricing.httpx, "get", boom)
    with pytest.raises(AzureUnavailable):
        azure_pricing.vm_hourly("large")


def test_unknown_size_raises():
    with pytest.raises(AzureUnavailable):
        azure_pricing.vm_hourly("humongous")
