"""Increment 2.2 checks: the OCI pricing adapter parses, converts, caches, fails safe.

The real OCI API is never called here — httpx is mocked.
"""

import pytest

from api.adapters import oci_pricing
from api.adapters.oci_pricing import OCIUnavailable


@pytest.fixture(autouse=True)
def clear_cache():
    oci_pricing._cache.clear()
    yield
    oci_pricing._cache.clear()


def _item(part, value):
    return {
        "partNumber": part,
        "currencyCodeLocalizations": [
            {"currencyCode": "AED", "prices": [{"model": "PAY_AS_YOU_GO", "value": value}]}
        ],
    }


ITEMS = [
    _item(oci_pricing.PART_OCPU_HOUR, 0.03),        # per OCPU/hour
    _item(oci_pricing.PART_MEMORY_GB_HOUR, 0.002),  # per GB/hour
    _item(oci_pricing.PART_STORAGE_GB_MONTH, 0.0255),  # per GB/month
    _item("B99999", 1.23),  # noise
]


class _Resp:
    def __init__(self, items):
        self._items = items

    def raise_for_status(self):
        return None

    def json(self):
        return {"items": self._items}


def test_rates_are_parsed_and_ocpu_converted(monkeypatch):
    monkeypatch.setattr(oci_pricing.httpx, "get", lambda *a, **k: _Resp(ITEMS))
    rates = oci_pricing.rates()
    # OCPU 0.03 / 2 vCPU = 0.015 per vCPU/hour.
    assert rates["vcpu-hour"] == pytest.approx(0.015)
    assert rates["memory-gb-hour"] == pytest.approx(0.002)
    assert rates["storage-gb-month"] == pytest.approx(0.0255)


def test_rates_cached(monkeypatch):
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp(ITEMS)

    monkeypatch.setattr(oci_pricing.httpx, "get", fake_get)
    oci_pricing.rates()
    oci_pricing.rates()
    assert calls["n"] == 1


def test_missing_part_raises(monkeypatch):
    monkeypatch.setattr(oci_pricing.httpx, "get", lambda *a, **k: _Resp([ITEMS[0]]))
    with pytest.raises(OCIUnavailable):
        oci_pricing.rates()


def test_http_error_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(oci_pricing.httpx, "get", boom)
    with pytest.raises(OCIUnavailable):
        oci_pricing.rates()
