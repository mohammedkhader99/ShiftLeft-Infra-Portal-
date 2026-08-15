"""An OS whose packages this network cannot fetch is not a choice.

WHY THIS FILE EXISTS. REQ-2026-0134 asked for Apache and nginx in one request, on
one subnet. Apache installed httpd 2.4.62 and served on :80. nginx reported
`nginx NOT INSTALLED`. The difference was never the software: Oracle Linux's yum
mirrors sit INSIDE the Oracle Services Network and the subnet had a service
gateway, while Ubuntu's apt repositories are on the public internet and the
subnet had no NAT gateway.

The portal offered that Ubuntu image, priced it, sent it for approval, had it
approved, and built a machine that could never have worked. Nothing in the chain
knew what the network could reach, so nothing could say so.

Standing rule from the user, and this is the third thing it has caught:
"you have to ensure make validation tell the truth first, so the form refuses the
user request by displaying valid reason and guide accordingly."

The tests below hold the refusal to that standard — it must be grounded in the
real route table, must survive to the server side, and must tell the requester
what to do instead.
"""

from __future__ import annotations

import pytest

from api import network_egress

INTERNET = {"known": True, "subnet_name": "AI-ShiftL-DEV-VM-APP-SUBNET",
            "internet": True, "oracle_services": True,
            "families": ["debian", "rhel", "suse"], "reason": ""}
SERVICES_ONLY = {"known": True, "subnet_name": "AI-ShiftL-DEV-VM-APP-SUBNET",
                 "internet": False, "oracle_services": True,
                 "families": ["rhel"], "reason": ""}
NOTHING = {"known": True, "subnet_name": "AI-ShiftL-DEV-DB-SUBNET",
           "internet": False, "oracle_services": False, "families": [], "reason": ""}
UNKNOWN = {"known": False, "families": [],
           "reason": "the subnet's routing could not be read"}


@pytest.fixture(autouse=True)
def _clean():
    network_egress.reset()
    yield
    network_egress.reset()


def _says(answer):
    return lambda: answer


# --- The rule, grounded in what the network can actually reach ----------------

def test_ubuntu_is_refused_where_there_is_no_route_to_the_internet():
    """THE case. This exact subnet built an empty Ubuntu machine."""
    assert network_egress.can_install("debian", _says(SERVICES_ONLY)) is False


def test_oracle_linux_is_allowed_on_the_same_subnet():
    """A service gateway IS enough for Oracle's mirrors — and the refusal must
    not spread to the OS that genuinely works, or it stops being true."""
    assert network_egress.can_install("rhel", _says(SERVICES_ONLY)) is True


def test_a_nat_gateway_lets_ubuntu_back_in():
    """The fix the user applied. The rule has to notice it, or it becomes a
    permanent ban on Ubuntu that outlives its reason."""
    assert network_egress.can_install("debian", _says(INTERNET)) is True


def test_a_subnet_that_reaches_nothing_refuses_both():
    """The database and Kubernetes subnets are like this today."""
    for family in ("debian", "rhel"):
        assert network_egress.can_install(family, _says(NOTHING)) is False


# --- Unknown is not a verdict -------------------------------------------------

def test_an_unreadable_network_does_not_refuse_everything():
    """Refusing every image because a lookup failed would take the form down, and
    the machine's own boot report still catches what this misses."""
    assert network_egress.can_install("debian", _says(UNKNOWN)) is True


def test_an_unreadable_network_says_so_rather_than_looking_permissive():
    """The failure this project keeps repeating: a check that silently stopped
    checking is indistinguishable from one that found nothing wrong."""
    network_egress.can_install("debian", _says(UNKNOWN))
    assert network_egress.known() is False


def test_a_readable_network_is_reported_as_known():
    network_egress.can_install("debian", _says(SERVICES_ONLY))
    assert network_egress.known() is True


def test_an_unreachable_orchestrator_does_not_crash_the_form():
    def explode():
        raise RuntimeError("connection refused")
    assert network_egress.can_install("debian", explode) is True


# --- The refusal has to be actionable ----------------------------------------

def test_the_refusal_explains_and_guides():
    """A refusal that only says no teaches nobody anything and gets worked
    around. It must name the cause, the alternative, and the fix."""
    message = network_egress.guidance("debian", _says(SERVICES_ONLY))
    assert "public internet" in message, "it must say WHY"
    assert "Oracle Linux" in message, "it must offer the OS that does work"
    assert "NAT gateway" in message, "it must say what would fix it"


def test_the_refusal_names_the_actual_subnet():
    """'this environment's subnet' is true and useless to whoever has to change
    it. The real name is what makes the guidance a ticket someone can raise."""
    assert "AI-ShiftL-DEV-VM-APP-SUBNET" in network_egress.guidance(
        "debian", _says(SERVICES_ONLY))


def test_the_refusal_for_oracle_linux_is_not_the_ubuntu_one():
    """Where even Oracle's mirrors are unreachable, telling someone to pick
    Oracle Linux instead is advice that cannot work."""
    message = network_egress.guidance("rhel", _says(NOTHING))
    assert "NAT gateway" not in message or "Oracle" in message
    assert "service gateway" in message


# --- Caching ------------------------------------------------------------------

def test_the_answer_is_cached_rather_than_fetched_per_dropdown_entry():
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return SERVICES_ONLY
    for _ in range(20):
        network_egress.can_install("debian", counted)
    assert calls["n"] == 1


def test_the_cache_expires_so_adding_a_gateway_takes_effect(monkeypatch):
    """Someone who adds a NAT gateway must not have to restart the portal to be
    able to request Ubuntu."""
    monkeypatch.setenv("NETWORK_EGRESS_TTL_SECONDS", "30")
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return SERVICES_ONLY if calls["n"] == 1 else INTERNET
    assert network_egress.can_install("debian", counted) is False
    monkeypatch.setattr(network_egress.time, "time",
                        lambda: network_egress._fetched_at + 31)
    assert network_egress.can_install("debian", counted) is True


# --- End to end: through the form and through the authority -------------------

@pytest.fixture()
def db():
    """The same in-memory catalogue the other validation tests use: one Oracle
    Linux image and one Ubuntu image, which is exactly the choice REQ-2026-0134
    got wrong."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api import cloud_options
    from db.seed import seed
    from db.session import Base

    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    cloud_options.refresh(session, lambda: {
        "ok": True, "mode": "mock", "shapes": [], "shapes_available": 0,
        "images_available": 2,
        "images": [
            {"ocid": "ocid1.image..ol9", "name": "Oracle-Linux-9.8-2026.07.20-0",
             "os": "Oracle Linux", "os_version": "9", "os_family": "rhel"},
            {"ocid": "ocid1.image..ubuntu", "name": "Canonical-Ubuntu-24.04-2026.07.17-0",
             "os": "Canonical Ubuntu", "os_version": "24.04", "os_family": "debian"},
        ],
    })
    yield session
    session.close()


def _no_internet(monkeypatch):
    """Put the portal back on the subnet REQ-2026-0134 was built in."""
    from api import component_options
    network_egress.reset()
    monkeypatch.setattr(component_options, "_fetch_egress", lambda: SERVICES_ONLY)


def _images(db, code):
    from api import component_options
    fields = component_options.options_for(db, code, "oci")["fields"]
    return [o["label"] for o in fields.get("image", {}).get("options", [])]


def test_the_ubuntu_image_is_not_offered_at_all(db, monkeypatch):
    """Prevent the wrong choice rather than refuse it afterwards — by submit time
    the requester has filled in a whole form, and a rejection then reads as the
    portal changing its mind."""
    _no_internet(monkeypatch)
    offered = _images(db, "nginx")
    assert offered, "the dropdown must not be emptied"
    assert not any("Ubuntu" in image for image in offered)
    assert any("Oracle-Linux" in image for image in offered)


def test_the_ubuntu_image_comes_back_once_there_is_a_route(db, monkeypatch):
    """nginx really does run on Ubuntu. The refusal is about the NETWORK, and it
    has to disappear the moment the network changes — the user added a NAT
    gateway for precisely this."""
    from api import component_options
    network_egress.reset()
    monkeypatch.setattr(component_options, "_fetch_egress", lambda: INTERNET)
    assert any("Ubuntu" in image for image in _images(db, "nginx"))


def test_the_server_refuses_it_even_if_the_dropdown_is_bypassed(db, monkeypatch):
    """The client holds no authority (CLAUDE.md). A stale draft, a re-submitted
    form, or a value posted straight to the API must meet the same answer."""
    from api import component_options
    _no_internet(monkeypatch)
    errors = component_options.validate(db, "nginx", "oci",
                                        {"image": "ocid1.image..ubuntu"})
    assert "image" in errors
    assert "public internet" in errors["image"]
    assert "NAT gateway" in errors["image"]


def test_oracle_linux_still_passes_on_the_same_subnet(db, monkeypatch):
    """The refusal must not spread to the OS that genuinely works there."""
    from api import component_options
    _no_internet(monkeypatch)
    assert component_options.validate(db, "nginx", "oci",
                                      {"image": "ocid1.image..ol9"}) == {}


def test_a_bucket_is_unaffected_by_the_network_rule(db, monkeypatch):
    """Object storage installs nothing on a machine, so no OS and no repository
    is involved. Reading its empty declaration as 'supports nothing' has already
    refused every image for buckets once."""
    from api import component_options
    _no_internet(monkeypatch)
    assert component_options.validate(db, "objectstorage", "oci", {}) == {}


def test_the_form_reports_whether_the_network_was_checked(db, monkeypatch):
    """An inert check must be visible rather than looking permissive."""
    from api import component_options
    _no_internet(monkeypatch)
    component_options.options_for(db, "nginx", "oci")
    assert component_options.options_for(db, "nginx", "oci")["network_known"] is True
