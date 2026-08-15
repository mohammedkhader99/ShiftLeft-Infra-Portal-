"""Reading what a subnet can reach, from the route table rather than from hope.

The distinction this module exists to draw is invisible from anywhere else in the
system: a subnet with a service gateway and no NAT can install Oracle Linux
packages and cannot install Ubuntu ones. REQ-2026-0134 demonstrated it on real
machines — httpd 2.4.62 serving on :80, and `nginx NOT INSTALLED` — in a single
request, on a single subnet.
"""

from __future__ import annotations

import pytest

from orchestrator import network_egress


class _Rule:
    def __init__(self, destination, entity, destination_type="CIDR_BLOCK"):
        self.destination = destination
        self.network_entity_id = entity
        self.destination_type = destination_type


class _Named:
    def __init__(self, display_name, **kw):
        self.display_name = display_name
        self.__dict__.update(kw)


class _Client:
    """Just enough of the OCI network client to answer the two calls made."""

    def __init__(self, rules, subnet_name="SUBNET", fail=False):
        self._rules, self._name, self._fail = rules, subnet_name, fail

    def get_subnet(self, _ocid):
        if self._fail:
            raise RuntimeError("NotAuthorizedOrNotFound")
        return _Named("resp", data=_Named(self._name, route_table_id="rt"))

    def get_route_table(self, _ocid):
        return _Named("resp", data=_Named("RT", route_rules=self._rules))


NAT = "ocid1.natgateway.oc1.me-dubai-1.aaaa"
IGW = "ocid1.internetgateway.oc1.me-dubai-1.aaaa"
SGW = "ocid1.servicegateway.oc1.me-dubai-1.aaaa"
ALL_SERVICES = "all-dxb-services-in-oracle-services-network"
OBJECT_STORAGE_ONLY = "oci-dxb-objectstorage"


def _egress(rules, **kw):
    return network_egress.egress_for_subnet("ocid1.subnet.x", _Client(rules, **kw))


# --- The real tenancy, before and after the fix -------------------------------

def test_service_gateway_only_installs_oracle_linux_and_not_ubuntu():
    """AI-ShiftLeft-DEV-VCN as it stood when REQ-2026-0134 was built."""
    result = _egress([_Rule(ALL_SERVICES, SGW, "SERVICE_CIDR_BLOCK")])
    assert result["known"] is True
    assert result["families"] == ["rhel"]
    assert result["internet"] is False and result["oracle_services"] is True


def test_adding_a_nat_gateway_admits_ubuntu():
    """The change the user applied. Both families become installable."""
    result = _egress([_Rule(ALL_SERVICES, SGW, "SERVICE_CIDR_BLOCK"),
                      _Rule("0.0.0.0/0", NAT)])
    assert set(result["families"]) == {"rhel", "debian", "suse"}
    assert result["internet"] is True


def test_an_internet_gateway_counts_as_much_as_a_nat_one():
    result = _egress([_Rule("0.0.0.0/0", IGW)])
    assert "debian" in result["families"]


def test_the_internet_alone_reaches_oracles_mirrors_too():
    """Oracle's yum servers are also on the public internet, so a subnet with a
    NAT and no service gateway can still install Oracle Linux."""
    result = _egress([_Rule("0.0.0.0/0", NAT)])
    assert "rhel" in result["families"]


def test_a_subnet_with_no_routes_installs_nothing():
    """The database and Kubernetes subnets. A machine built there comes up bare
    whatever image it is given."""
    assert _egress([])["families"] == []


# --- The distinctions that would be easy to get wrong -------------------------

def test_an_object_storage_only_service_gateway_is_not_enough():
    """A gateway scoped to Object Storage lets a machine FILE ITS BOOT REPORT and
    install nothing at all — the two look identical from a distance, and reading
    them as the same thing would bless a subnet that cannot install anything."""
    result = _egress([_Rule(OBJECT_STORAGE_ONLY, SGW, "SERVICE_CIDR_BLOCK")])
    assert result["oracle_services"] is False
    assert result["families"] == []


def test_a_route_to_somewhere_specific_is_not_a_route_to_the_internet():
    """A NAT gateway used for one peered range does not make apt reachable."""
    result = _egress([_Rule("10.99.0.0/16", NAT)])
    assert result["internet"] is False


def test_a_local_route_is_not_egress():
    result = _egress([_Rule("10.56.32.0/19", "ocid1.drg.oc1.x")])
    assert result["families"] == []


# --- It must never break the form ---------------------------------------------

def test_an_unreadable_subnet_reports_unknown_rather_than_empty():
    """"Nothing can install here" and "we could not look" are opposite facts.
    Returning the first for the second would refuse every image on the form."""
    result = _egress([], fail=True)
    assert result["known"] is False
    assert result["reason"], "it has to say why it could not judge"


def test_no_subnet_configured_is_also_unknown():
    assert network_egress.egress_for_subnet("")["known"] is False


def test_the_lookup_never_raises():
    """It runs while a requester is typing. An exception here is a broken page."""
    class Exploding:
        def get_subnet(self, _):
            raise ValueError("boom")
    assert network_egress.egress_for_subnet("x", Exploding())["known"] is False


# --- The guidance ------------------------------------------------------------

def test_guidance_names_the_subnet_and_the_way_out():
    result = _egress([_Rule(ALL_SERVICES, SGW, "SERVICE_CIDR_BLOCK")],
                     subnet_name="AI-ShiftL-DEV-VM-APP-SUBNET")
    message = network_egress.guidance("debian", result)
    assert "AI-ShiftL-DEV-VM-APP-SUBNET" in message
    assert "NAT gateway" in message
    assert "Oracle Linux" in message


@pytest.mark.parametrize("family", ["rhel", "debian", "suse"])
def test_every_family_the_portal_offers_has_a_declared_source(family):
    """A family with no entry would silently be treated as needing nothing, and
    every image of it would be offered on a subnet that cannot install it."""
    assert family in network_egress.FAMILY_NEEDS


# --- The endpoint, not just the module ---------------------------------------
#
# Everything above exercises network_egress directly, and all of it passed while
# the ENDPOINT raised AttributeError on its first real call: the handler was
# named `network_egress`, which at module level rebinds the imported module to
# the function, so `network_egress.report()` inside it resolved to the function.
#
# A unit test of the module could never have seen that. These call the endpoint.

def _call(monkeypatch, mode="apply", answer=None):
    import asyncio

    import orchestrator.main as omain
    from orchestrator import provisioner

    monkeypatch.setattr(provisioner, "provision_mode", lambda: mode)
    monkeypatch.setattr(omain, "verify", lambda *a, **k: True)
    if answer is not None:
        monkeypatch.setattr(omain.network_egress, "report", lambda: answer)

    class _Request:
        headers = {"X-Signature": "sig"}

        async def body(self):
            return b"{}"

    return asyncio.run(omain.network_egress_report(_Request()))


def test_the_endpoint_returns_what_the_module_found(monkeypatch):
    """THE regression. This failed with AttributeError in production while every
    test in this file passed."""
    answer = {"known": True, "families": ["rhel"], "subnet_name": "S"}
    assert _call(monkeypatch, answer=answer) == answer


def test_the_endpoint_still_resolves_the_module_not_itself(monkeypatch):
    """Stated separately because the shape of the bug — a handler shadowing the
    module it calls — is invisible in a passing unit test and silent until the
    first real request."""
    import orchestrator.main as omain
    assert hasattr(omain.network_egress, "report"), (
        "orchestrator.main.network_egress is not the module any more — a handler "
        "or variable has shadowed it")


def test_mock_mode_reports_no_network_limits(monkeypatch):
    """Nothing is built, so nothing constrains it. Reporting 'unknown' here would
    put a warning on every screen of the demo path."""
    result = _call(monkeypatch, mode="mock")
    assert result["known"] is False
    assert result["families"] == []
    assert result["reason"]


def test_an_unsigned_request_is_refused(monkeypatch):
    import asyncio

    import orchestrator.main as omain
    from fastapi import HTTPException

    monkeypatch.setattr(omain, "verify", lambda *a, **k: False)

    class _Request:
        headers: dict = {}

        async def body(self):
            return b"{}"

    with pytest.raises(HTTPException) as raised:
        asyncio.run(omain.network_egress_report(_Request()))
    assert raised.value.status_code == 401


def test_no_endpoint_shadows_a_module_the_orchestrator_imports():
    """The general form of the bug above, checked for every module.

    `@app.post(...)` does not rename the function it decorates, so a handler
    called `configure` or `provisioner` would replace that module in the module
    namespace and every other caller of it would break — at runtime, silently,
    and nowhere near the handler that caused it.
    """
    import ast
    import pathlib
    from types import ModuleType

    import orchestrator.main as omain

    source = pathlib.Path(omain.__file__).read_text(encoding="utf-8")
    imported = {
        alias.asname or alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            ("orchestrator", "common"))
        for alias in node.names
    }
    assert imported, "no imports found — this guard has stopped seeing them"
    shadowed = [
        name for name in sorted(imported)
        if hasattr(omain, name)
        and not isinstance(getattr(omain, name), ModuleType)
        and getattr(omain, name).__class__.__name__ == "function"
        and getattr(getattr(omain, name), "__module__", "") == omain.__name__
    ]
    assert not shadowed, (
        f"these imported names have been replaced by functions defined in "
        f"orchestrator/main.py: {shadowed}")
