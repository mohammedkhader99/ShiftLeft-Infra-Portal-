"""Validation must be grounded in what actually BUILDS a thing.

The form offered Apache on an Ubuntu image, and would have booted a VM with no
web server on it and called it provisioned. The rule refusing that combination
was already written — it just asked the wrong source.

`configure.py` carries a Debian recipe for apache. Apache is built by
`oci/apache-httpd`, which renders its own Red Hat-only cloud-init (package
`httpd`, `firewall-cmd`, `/etc/httpd/...`) and never calls `configure.py` at all.
So the honesty check answered confidently about a code path nothing executes.

These tests pin the direction of the question: ask the builder, not the table
that merely looks authoritative.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import cloud_options, component_options
from db.seed import seed
from db.session import Base
from orchestrator import blueprint_registry, configure


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
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


# --- The question is asked of the builder ------------------------------------

def test_a_dedicated_blueprint_overrides_the_generic_recipe_table():
    """THE bug. configure.py says apache runs on Debian; the module that builds
    it says Red Hat only, and the module is the one that runs."""
    assert "debian" in configure.supported_families("apache"), (
        "precondition: the generic table still claims Debian for apache")
    assert component_options.supported_families("apache") == {"rhel"}, (
        "the answer must come from oci/apache-httpd, not configure.py")


def test_a_technology_no_blueprint_claims_gets_no_os_opinion():
    """mongodb is in the catalogue and no blueprint builds it, so the portal has
    no view on which operating systems suit it.

    The API says "no opinion" rather than inventing one, and it cannot do
    otherwise: the API image does not contain the orchestrator, so configure.py
    is not something it can consult. An earlier version imported it anyway inside
    a try/except, which worked under pytest and raised in the container — see
    test_api_does_not_import_orchestrator.

    This used to be asserted with java21, which had a recipe in configure.py that
    NO blueprint listed — unreachable code, and a recipe that could never be
    booted could never be proven either. service-vm now declares it, so the
    example moved to a technology that genuinely has no recipe at all.
    """
    assert blueprint_registry.for_technology("mongodb") is None
    assert component_options.supported_families("mongodb") is None
    # ...and "no opinion" must not be read as "supports nothing".
    assert component_options.installable_on("mongodb", "debian") is True


def test_a_declared_recipe_gets_an_os_opinion_even_before_it_is_certified():
    """The other half. java21 has a recipe and service-vm now declares it, so the
    portal knows which families it can be configured on — while the catalogue
    badge stays manual until somebody certifies it.

    Capability and certification are different facts. Conflating them is what
    made the recipe unreachable: it could not be installed on a machine, so it
    could never be verified, so it could never be certified.
    """
    assert blueprint_registry.for_technology("java21") is not None
    assert component_options.supported_families("java21") == {"rhel", "debian"}


def test_the_generic_blueprint_agrees_with_the_table_it_delegates_to():
    """service-vm runs configure.py, so a manifest claiming more than the recipe
    table can deliver would be the same lie in the other direction."""
    declared = blueprint_registry.os_families_for_technology("nginx")
    assert declared is not None
    assert declared <= set(configure.FAMILIES)
    assert declared == configure.supported_families("nginx")


# --- The form and validation both act on it ----------------------------------

def test_apache_on_ubuntu_is_refused(db):
    errors = component_options.validate(db, "apache", "oci",
                                        {"image": "ocid1.image..ubuntu"})
    assert "image" in errors


def test_apache_on_oracle_linux_is_accepted(db):
    assert component_options.validate(db, "apache", "oci",
                                      {"image": "ocid1.image..ol9"}) == {}


def test_nginx_on_ubuntu_is_still_accepted(db):
    """The guard must not over-reach: nginx really does run on both."""
    assert component_options.validate(db, "nginx", "oci",
                                      {"image": "ocid1.image..ubuntu"}) == {}


def test_the_form_marks_the_combination_as_not_installable(db):
    out = component_options.options_for(db, "apache", "oci", "ocid1.image..ubuntu")
    assert out["installable"] is False
    assert out["os_family"] == "debian"


# --- The dropdown only offers images the technology actually runs on ---------

def _images(db, code):
    fields = component_options.options_for(db, code, "oci")["fields"]
    return [o["label"] for o in fields.get("image", {}).get("options", [])]


def test_apache_is_offered_only_the_images_it_is_certified_on(db):
    """Standing requirement (user, 2026-08-14): prevent the wrong choice rather
    than refuse it afterwards. By submit time the requester has filled in a whole
    form, and the rejection reads as the portal changing its mind."""
    offered = _images(db, "apache")
    assert offered == ["Oracle-Linux-9.8-2026.07.20-0"]
    assert not any("Ubuntu" in i for i in offered)


def test_nginx_is_offered_both_because_it_runs_on_both(db):
    offered = _images(db, "nginx")
    assert any("Oracle-Linux" in i for i in offered)
    assert any("Ubuntu" in i for i in offered)


def test_the_default_image_is_one_the_technology_can_use(db):
    """A default drawn from the unfiltered list would pre-select an image the
    requester cannot have, and the form would open already invalid."""
    fields = component_options.options_for(db, "apache", "oci")["fields"]
    default = fields["image"]["default"]
    assert component_options.validate(db, "apache", "oci", {"image": default}) == {}


def test_the_list_widens_by_itself_when_a_blueprint_gains_a_family(monkeypatch, db):
    """The requirement is explicitly about tomorrow as well as today: a component
    certified on another OS must gain those images without anyone editing a
    per-technology image list."""
    assert not any("Ubuntu" in i for i in _images(db, "apache"))
    monkeypatch.setattr(component_options, "supported_families",
                        lambda code: {"rhel", "debian"} if code == "apache"
                        else configure.supported_families(code))
    assert any("Ubuntu" in i for i in _images(db, "apache")), (
        "certifying apache on debian must widen its image list with no other change")


def test_a_technology_with_no_os_opinion_sees_every_image(db):
    """A bucket installs nothing on a machine, so filtering its images would hide
    valid choices for no reason."""
    assert len(_images(db, "oci-objectstorage")) == 2


def test_an_image_of_unrecognised_family_is_shown_not_hidden(db):
    """Our failure to recognise an OS is our gap. Hiding a legitimate image over
    it is worse than showing it and letting validation have the last word."""
    from db.models import ComponentOption
    from sqlalchemy import select
    row = db.scalars(select(ComponentOption).where(
        ComponentOption.field == "image")).first()
    row.attributes = {**(row.attributes or {}), "os_family": ""}
    db.commit()
    assert row.label in _images(db, "apache")


def test_hiding_an_image_does_not_replace_server_side_validation(db):
    """The dropdown is UX; the rule is the rule. A request naming a hidden image
    must still be refused (ARCHITECTURE P2 — the client holds no authority)."""
    assert not any("Ubuntu" in i for i in _images(db, "apache"))
    assert "image" in component_options.validate(
        db, "apache", "oci", {"image": "ocid1.image..ubuntu"})


# --- A refusal must guide, not just refuse -----------------------------------

def test_the_refusal_names_an_image_the_requester_can_actually_pick(db):
    """"it can install it on: rhel" is true and useless — nobody chooses "rhel"
    from a dropdown. The message has to name a real option."""
    message = component_options.validate(
        db, "apache", "oci", {"image": "ocid1.image..ubuntu"})["image"]
    assert "Oracle-Linux-9.8-2026.07.20-0" in message, "name a selectable image"
    assert "cannot be installed on this image" in message
    assert "remove apache" in message, "and the other way out of the problem"


# --- Nothing may stay silent about what it supports --------------------------

def test_every_blueprint_that_installs_software_declares_its_os_families():
    """A blueprint that says nothing is exactly how this happened: the Apache
    manifest had carried a COMMENT saying it needs a Red Hat image since the day
    it shipped, and a comment cannot refuse a request.
    """
    # Scoped to the blueprints that boot a machine. A bucket and a managed
    # database have no operating system for a requester to choose, so demanding
    # a declaration from them would be noise — and a guard that asks for
    # meaningless entries gets filled in with meaningless entries.
    silent = [
        bp["ref"] for bp in blueprint_registry.discover()
        if bp.get("name_var") == "instance_name" and not bp.get("os_families")
    ]
    assert not silent, (
        f"these blueprints boot a machine but declare no os_families: {silent}. "
        "Add os_families to the manifest — the portal cannot otherwise tell "
        "whether an image the requester picked is one they can actually use.")


def test_a_blueprint_with_no_operating_system_is_not_required_to_declare_one():
    """The other side of the rule: object storage has no OS, and forcing a
    declaration there would make the guard meaningless."""
    buckets = [bp for bp in blueprint_registry.discover()
               if bp.get("name_var") == "bucket_name"]
    assert buckets, "precondition: there are bucket blueprints"
    assert all(not bp.get("os_families") for bp in buckets)
    # ...and they make no OS claim, so nothing is refused on their account.
    for bp in buckets:
        for code in bp["builds"]:
            assert component_options.installable_on(code, "debian") is True


def test_declared_families_are_real_ones():
    for bp in blueprint_registry.discover():
        for family in (bp.get("os_families") or []):
            assert family in configure.FAMILIES, (
                f"{bp['ref']} declares unknown OS family {family!r}")
