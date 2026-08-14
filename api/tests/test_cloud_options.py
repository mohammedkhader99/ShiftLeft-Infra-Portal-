"""The hourly cloud option cache.

The form's dropdowns were a list a human typed into db/seed.py. This replaces the
machine-knowable part with what the tenancy really has.

The tests that matter are about what a refresh is allowed to DESTROY. A cache
that empties itself when the network blinks, or that overwrites the hand-curated
version lists no cloud API can answer, would be worse than the hard-coded list it
replaces — the form would silently stop offering things that are perfectly valid.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import cloud_options, component_options
from db.models import ComponentOption
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    yield session
    session.close()


def _fetched(**overrides) -> dict:
    """What the orchestrator returns for a healthy tenancy."""
    return {
        "ok": True,
        "mode": "mock",
        "shapes": [
            {"name": "VM.Standard.E4.Flex", "min_ocpus": 1, "max_ocpus": 64,
             "min_memory_gb": 1, "max_memory_gb": 1024},
            {"name": "VM.Standard2.1", "min_ocpus": 1, "max_ocpus": 1,
             "min_memory_gb": 15, "max_memory_gb": 15},
        ],
        "images": [
            {"ocid": "ocid1.image..ol9", "name": "Oracle-Linux-9.4-2026.01.31-0",
             "os": "Oracle Linux", "os_version": "9"},
            {"ocid": "ocid1.image..ol8", "name": "Oracle-Linux-8.10-2026.01.31-0",
             "os": "Oracle Linux", "os_version": "8"},
        ],
        "shapes_available": 40,
        "images_available": 300,
        **overrides,
    }


def _live_rows(db):
    return db.scalars(
        select(ComponentOption).where(ComponentOption.source == cloud_options.LIVE_SOURCE)
    ).all()


def _seed_versions(db):
    return db.scalars(
        select(ComponentOption).where(ComponentOption.source == "seed",
                                      ComponentOption.field == "version")
    ).all()


# --- A refresh caches what the cloud offers ----------------------------------

def test_a_refresh_caches_images_and_shapes(db):
    result = cloud_options.refresh(db, lambda: _fetched())
    assert result["ok"] is True
    rows = _live_rows(db)
    assert {r.field for r in rows} == {"image", "shape"}
    assert all(r.refreshed_at is not None for r in rows)


def test_the_image_becomes_a_dropdown_the_form_can_show(db):
    cloud_options.refresh(db, lambda: _fetched())
    fields = component_options.options_for(db, "nginx", "oci")["fields"]
    assert "image" in fields
    # Picked by display name, not by an unreadable OCID.
    assert fields["image"]["options"][0]["label"].startswith("Oracle-Linux-9")
    assert fields["image"]["options"][0]["value"].startswith("ocid1.image")


def test_an_image_is_offered_for_every_technology_not_stored_46_times(db):
    """It belongs to the machine, not to nginx."""
    cloud_options.refresh(db, lambda: _fetched())
    assert len([r for r in _live_rows(db) if r.field == "image"]) == 2
    for code in ("nginx", "redis7", "postgres16"):
        assert "image" in component_options.options_for(db, code, "oci")["fields"]


def test_shapes_are_cached_but_are_not_a_dropdown(db):
    """Five controls per component to serve a choice most requesters have no
    opinion about. Shapes are a validation input instead."""
    cloud_options.refresh(db, lambda: _fetched())
    assert [r for r in _live_rows(db) if r.field == "shape"]
    assert "shape" not in component_options.options_for(db, "nginx", "oci")["fields"]


# --- What a refresh may NOT destroy ------------------------------------------

def test_a_refresh_never_touches_the_hand_curated_rows(db):
    """No cloud API can say which nginx versions are installable, so the version
    lists are curated. A fetch that overwrote them would delete the only data the
    fetch cannot regenerate."""
    before = {(r.technology_code, r.value) for r in _seed_versions(db)}
    assert before, "the seed should have version rows"
    cloud_options.refresh(db, lambda: _fetched())
    assert {(r.technology_code, r.value) for r in _seed_versions(db)} == before


def test_an_unreachable_orchestrator_leaves_the_cache_alone(db):
    """Serving slightly stale options beats serving none."""
    cloud_options.refresh(db, lambda: _fetched())
    before = {r.value for r in _live_rows(db)}

    def boom():
        raise RuntimeError("connection refused")

    result = cloud_options.refresh(db, boom)
    assert result["ok"] is False
    assert "unreachable" in result["reason"]
    assert {r.value for r in _live_rows(db)} == before


def test_a_tenancy_it_cannot_read_leaves_the_cache_alone(db):
    cloud_options.refresh(db, lambda: _fetched())
    before = {r.value for r in _live_rows(db)}
    result = cloud_options.refresh(
        db, lambda: {"ok": False, "reason": "credentials rejected", "shapes": [], "images": []})
    assert result["ok"] is False
    assert {r.value for r in _live_rows(db)} == before


def test_an_empty_allowlist_does_not_wipe_the_cache(db):
    """An unset allowlist is a configuration gap, not a reason to lose data —
    and the message has to name the setting, or nobody can act on it."""
    cloud_options.refresh(db, lambda: _fetched())
    before = {r.value for r in _live_rows(db)}
    result = cloud_options.refresh(
        db, lambda: _fetched(shapes=[], images=[]))
    assert result["ok"] is False
    assert "OCI_SHAPE_ALLOWLIST" in result["reason"]
    assert {r.value for r in _live_rows(db)} == before


# --- What a refresh MUST destroy ---------------------------------------------

def test_a_withdrawn_shape_stops_being_offered(db):
    """The other half: if the fetch never deleted, removing a shape from the
    allowlist would leave it selectable forever."""
    cloud_options.refresh(db, lambda: _fetched())
    assert any(r.value == "VM.Standard2.1" for r in _live_rows(db))

    cloud_options.refresh(db, lambda: _fetched(
        shapes=[{"name": "VM.Standard.E4.Flex", "min_ocpus": 1, "max_ocpus": 64,
                 "min_memory_gb": 1, "max_memory_gb": 1024}]))
    assert not any(r.value == "VM.Standard2.1" for r in _live_rows(db))


def test_a_withdrawn_image_stops_being_offered(db):
    cloud_options.refresh(db, lambda: _fetched())
    cloud_options.refresh(db, lambda: _fetched(
        images=[{"ocid": "ocid1.image..ol9", "name": "Oracle-Linux-9.4-2026.01.31-0",
                 "os": "Oracle Linux", "os_version": "9"}]))
    images = component_options.options_for(db, "nginx", "oci")["fields"]["image"]["options"]
    assert [o["value"] for o in images] == ["ocid1.image..ol9"]


# --- Shapes as a validation rule ---------------------------------------------

def test_a_shape_that_cannot_hold_the_request_is_refused(db):
    """Each part is on its own dropdown and the combination is still unbuildable.
    Caught at request time rather than by a Terraform error after approval."""
    cloud_options.refresh(db, lambda: _fetched(
        shapes=[{"name": "VM.Standard2.1", "min_ocpus": 1, "max_ocpus": 1,
                 "min_memory_gb": 15, "max_memory_gb": 15}]))
    errors = component_options.validate(db, "nginx", "oci",
                                        {"vcpu": "16", "memory_gb": "128"})
    assert "vcpu" in errors
    assert "No approved machine shape" in errors["vcpu"]
    assert "VM.Standard2.1" in errors["vcpu"]


def test_a_shape_that_fits_is_accepted(db):
    cloud_options.refresh(db, lambda: _fetched())
    assert component_options.validate(db, "nginx", "oci",
                                      {"vcpu": "16", "memory_gb": "128"}) == {}


def test_no_cached_shapes_means_no_shape_check(db):
    """Before the fetch has ever run, absent data must not block every request —
    absence of evidence is not evidence the shape is too small."""
    assert component_options.validate(db, "nginx", "oci",
                                      {"vcpu": "16", "memory_gb": "128"}) == {}


# --- Operability --------------------------------------------------------------

def test_the_refresh_interval_has_a_floor(monkeypatch):
    """This calls a real cloud API on a timer; a mis-set 1 would hammer it."""
    monkeypatch.setenv("OCI_CATALOGUE_REFRESH_SECONDS", "1")
    assert cloud_options.refresh_interval_seconds() == 300
    monkeypatch.setenv("OCI_CATALOGUE_REFRESH_SECONDS", "not-a-number")
    assert cloud_options.refresh_interval_seconds() == 3600


def test_the_cache_is_off_by_default(monkeypatch):
    monkeypatch.delenv("OCI_CATALOGUE_ENABLED", raising=False)
    assert cloud_options.enabled() is False


def test_last_refreshed_is_answerable(db):
    assert cloud_options.last_refreshed(db) is None
    cloud_options.refresh(db, lambda: _fetched())
    assert cloud_options.last_refreshed(db) is not None


def test_the_columns_are_wide_enough_for_a_real_ocid():
    """SQLite ignores VARCHAR limits, so the tests above would happily store a
    200-character value in a 64-character column and only PRODUCTION would fail.
    Assert the declared width instead of relying on the test database.

    A real image OCID is about 100 characters:
    ocid1.image.oc1.me-dubai-1.aaaaaaaa<60 more>
    """
    columns = ComponentOption.__table__.columns
    assert columns["value"].type.length >= 120, "an image OCID will not fit"
    assert columns["label"].type.length >= 120


def test_a_realistic_ocid_round_trips(db):
    """The same thing from the other end: store one and read it back."""
    ocid = "ocid1.image.oc1.me-dubai-1." + "a" * 60
    cloud_options.refresh(db, lambda: _fetched(images=[
        {"ocid": ocid, "name": "Oracle-Linux-9.4-2026.01.31-0",
         "os": "Oracle Linux", "os_version": "9"}]))
    options = component_options.options_for(db, "nginx", "oci")["fields"]["image"]["options"]
    assert options[0]["value"] == ocid
