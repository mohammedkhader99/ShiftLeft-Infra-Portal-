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


def _tiered(part, free_upto, value):
    """A part whose first band is free — Object Storage is billed this way."""
    return {
        "partNumber": part,
        "currencyCodeLocalizations": [
            {"currencyCode": "AED", "prices": [
                {"model": "PAY_AS_YOU_GO", "value": 0, "rangeMin": 0, "rangeMax": free_upto},
                {"model": "PAY_AS_YOU_GO", "value": value, "rangeMin": free_upto,
                 "rangeMax": 999999999},
            ]}
        ],
    }


ITEMS = [
    _item(oci_pricing.PART_OCPU_HOUR, 0.03),        # per OCPU/hour
    _item(oci_pricing.PART_MEMORY_GB_HOUR, 0.002),  # per GB/hour
    _item(oci_pricing.PART_STORAGE_GB_MONTH, 0.0255),  # per GB/month
    # The kinds that are not virtual machines.
    _tiered(oci_pricing.PART_BUCKET_GB_MONTH, 10, 0.0936615),
    _item(oci_pricing.PART_OKE_CLUSTER_HOUR, 0.3673),
    _item(oci_pricing.PART_PSQL_OCPU_HOUR, 0.36),
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
    # Not-a-VM rates, added because a bucket was being billed a VM's compute.
    assert rates["oke-cluster-hour"] == pytest.approx(0.3673)
    # Managed PostgreSQL is published per OCPU and stored per vCPU, like compute.
    assert rates["psql-vcpu-hour"] == pytest.approx(0.18)


def test_a_tiered_part_reports_the_marginal_rate_not_the_free_one(monkeypatch):
    """FOUND 2026-08-21. Object Storage publishes 0.00 for the first 10 GB and
    0.0936615 beyond it. Taking the first price listed reported object storage
    as free of charge — a confident zero, which is the worst kind of wrong for
    something a cost ceiling is meant to guard."""
    monkeypatch.setattr(oci_pricing.httpx, "get", lambda *a, **k: _Resp(ITEMS))
    rates = oci_pricing.rates()
    assert rates["bucket-storage-gb-month"] == pytest.approx(0.0936615)
    assert rates["bucket-free-gb"] == pytest.approx(10)


def test_a_missing_not_a_vm_part_fails_rather_than_pricing_it_at_zero(monkeypatch):
    """An absent part must raise, so the caller falls back to the cached rate
    cards. Defaulting it to 0.0 would price a cluster's control plane at
    nothing and look like a bargain."""
    partial = [i for i in ITEMS if i["partNumber"] != oci_pricing.PART_OKE_CLUSTER_HOUR]
    monkeypatch.setattr(oci_pricing.httpx, "get", lambda *a, **k: _Resp(partial))
    with pytest.raises(OCIUnavailable):
        oci_pricing.rates()


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
