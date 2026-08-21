"""Where the catalogue and the cloud disagree about what exists (C3).

The portal sells `postgres16`. The CLOUD decides what it will still build, and
changes that without telling anyone. On 2026-08-17 the OKE module pinned
Kubernetes v1.29.1, OCI had retired it, and nothing in the portal knew until four
real requests had failed at apply. Somebody had to notice; nobody did.

The direction that matters is the unobvious one. Falling behind is an
opportunity. Selling a version the cloud has retired is a promise the portal
cannot keep, discovered by a user whose approved request fails.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api import catalogue_sync as cs
from api.main import app, get_session
from db.models import AuditLog, Technology
from db.seed import seed
from db.session import Base


# --- The comparison itself ---------------------------------------------------

def test_the_acceptance_criterion(monkeypatch):
    """From the proposal, verbatim: OCI offers PostgreSQL 13-18 while the
    catalogue sells only 16; the sync raises 17 and 18 without a human noticing
    first."""
    gaps = cs.find_gaps({"postgres": ["13", "14", "15", "16", "17", "18"]},
                        ["postgres16", "nginx", "redis7"])
    assert len(gaps) == 1
    assert gaps[0]["newer"] == ["17", "18"]
    assert gaps[0]["retired"] == []
    assert gaps[0]["severity"] == "behind"


def test_a_retired_version_is_the_serious_one():
    """REQ-2026-0148 replayed. The portal sold a Kubernetes version OCI had
    retired, and found out when a real request failed at apply."""
    gaps = cs.find_gaps({"kubernetes": ["v1.33.10", "v1.34.2", "v1.36.1"]}, [],
                        pinned_kubernetes="v1.29.1")
    assert len(gaps) == 1
    assert gaps[0]["severity"] == "retired"
    assert "v1.29.1" in gaps[0]["note"]
    assert "fail at apply" in gaps[0]["note"]


def test_an_unpinned_kubernetes_version_can_never_be_stale():
    """Left unset, the module resolves the newest version OCI offers at build
    time — so there is nothing to go stale, and reporting a gap would be noise."""
    assert cs.find_gaps({"kubernetes": ["v1.36.1"]}, [], pinned_kubernetes="") == []


def test_older_versions_we_choose_not_to_sell_are_not_gaps():
    """Not selling PostgreSQL 13 is a decision, not an oversight. Reporting it
    would bury the two findings that matter."""
    assert cs.find_gaps({"postgres": ["13", "14", "15", "16"]}, ["postgres16"]) == []


def test_an_unreachable_cloud_says_nothing_rather_than_everything(monkeypatch):
    """THE failure to avoid. `None` means "we could not ask", and must never read
    as "the cloud offers nothing" — a network blip would otherwise report the
    entire catalogue as retired."""
    assert cs.find_gaps({"postgres": None}, ["postgres16"]) == []
    assert cs.compare("postgres", None, ["16"]) is None
    # An empty list is different: the cloud answered, and answered nothing.
    assert cs.compare("postgres", [], ["16"])["retired"] == ["16"]


def test_versions_are_ordered_numerically_not_alphabetically():
    """String order puts 9 after 18, and v1.33.1 after v1.33.10 — the mistake
    that picked Oracle Linux 7 out of a list of 120 images."""
    assert cs.version_key("18") > cs.version_key("9")
    assert cs.version_key("v1.33.10") > cs.version_key("v1.33.1")
    gaps = cs.find_gaps({"postgres": ["9", "16", "18"]}, ["postgres16"])
    assert gaps[0]["newer"] == ["18"], gaps[0]["newer"]


def test_retirements_are_listed_before_opportunities():
    gaps = cs.find_gaps({"postgres": ["16", "17", "18"], "kubernetes": ["v1.36.1"]},
                        ["postgres16"], pinned_kubernetes="v1.29.1")
    assert [g["severity"] for g in gaps] == ["retired", "behind"]


# --- Through the endpoint ----------------------------------------------------

@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)          # already ships postgres16, which is what these tests compare
    s.commit()
    s.factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield s
    s.close()


@pytest.fixture()
def client(db):
    def override():
        yield db
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def orchestrator_offers(monkeypatch, families, ok=True):
    def fake_post(body, signature, path="/provision"):
        if not ok:
            return None, "unreachable"
        return type("R", (), {"status_code": 200,
                              "json": lambda self=None: {"families": families}})(), None
    monkeypatch.setattr(main, "_post_to_orchestrator", fake_post)


def test_the_endpoint_reports_the_gap(client, monkeypatch):
    orchestrator_offers(monkeypatch, {"postgres": ["16", "17", "18"]})
    body = client.get("/api/catalogue/gaps").json()
    assert body["reachable"] is True
    assert body["behind"] == 1 and body["retired"] == 0
    assert body["gaps"][0]["newer"] == ["17", "18"]


def test_the_endpoint_distinguishes_unreachable_from_no_gaps(client, monkeypatch):
    """'We could not ask' and 'everything is current' must not look the same —
    one is worth investigating and the other is a clean bill of health."""
    orchestrator_offers(monkeypatch, {}, ok=False)
    body = client.get("/api/catalogue/gaps").json()
    assert body["reachable"] is False
    assert body["gaps"] == []
    assert "could not be reached" in body["note"]


def test_a_family_that_could_not_be_asked_is_named(client, monkeypatch):
    """Partial silence is reported rather than hidden: an admin should know the
    PostgreSQL answer is missing, not assume it was clean."""
    orchestrator_offers(monkeypatch, {"postgres": None, "kubernetes": ["v1.36.1"]})
    body = client.get("/api/catalogue/gaps").json()
    assert body["families_unreachable"] == ["postgres"]


# --- The sweep notices once --------------------------------------------------

def test_a_gap_is_audited_once_not_every_sweep(db, monkeypatch):
    """A retirement is worth an audit entry the moment it appears. Repeating it
    every thirty seconds would bury it in its own noise."""
    orchestrator_offers(monkeypatch, {"postgres": ["16", "17"]})

    main._sweep_catalogue_gaps(db)
    main._sweep_catalogue_gaps(db)
    main._sweep_catalogue_gaps(db)

    entries = [a for a in db.scalars(
        __import__("sqlalchemy").select(AuditLog)).all() if a.event == "catalogue.gap"]
    assert len(entries) == 1, f"{len(entries)} entries — the sweep is repeating itself"


def test_a_changed_gap_is_audited_again(db, monkeypatch):
    """Noticing once must not mean never noticing again: a NEW retirement is a
    new fact."""
    from sqlalchemy import select as _select

    orchestrator_offers(monkeypatch, {"postgres": ["16", "17"]})
    main._sweep_catalogue_gaps(db)

    orchestrator_offers(monkeypatch, {"postgres": ["17", "18"]})   # 16 now retired
    main._sweep_catalogue_gaps(db)

    entries = [a for a in db.scalars(_select(AuditLog)).all()
               if a.event == "catalogue.gap"]
    assert len(entries) == 2
    assert entries[-1].detail["gaps"][0]["retired"] == ["16"]
