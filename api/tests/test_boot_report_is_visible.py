"""A requester can read what their machine said about itself.

The portal already collects these reports and uses them to decide whether a
request provisioned — then discards them. They answer "what did I actually get"
better than any status can: the OS, the package versions, whether the service is
running, whether the port answers, whether the firewall is open.

The property these tests exist to protect is that the REPORT travels and the
CREDENTIAL does not. The reports live behind a pre-authenticated OCI URL, which
is a bearer token in URL form: anyone who ever saw it could read every machine's
report, forever, without signing in. So the text is proxied and the URL stays in
the orchestrator.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.main import app, get_session
from db.models import Blueprint, ProvisionedResource, Request, RequestComponent
from db.seed import seed
from db.session import Base

# A real report, copied verbatim from REQ-2026-0163 on 2026-08-21.
REAL_REPORT = (
    "os_family=rhel\ntechnologies=apache\nos=Oracle Linux Server 9.8\n"
    "--- packages ---\nhttpd-2.4.62-13.0.1.el9_8.5.x86_64\n\n"
    "--- services ---\nhttpd=active\n--- ports ---\nhttp_80=200\n"
    "LISTEN 0      511                *:80               *:*   \nyes\n"
    "firewall_80=open\n--- first-boot log ---\n"
    "PORTAL: first-boot configuration finished\n"
)

# What the orchestrator holds and must never hand over.
PAR_URL = "https://objectstorage.me-dubai-1.oraclecloud.com/p/SeCrEtTOkEn123/n/x/b/reports/o/"


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.add(Blueprint(technology_code="apache", deployment_target="oci",
                    blueprint_ref="oci/apache-httpd", status="certified",
                    resource_kind="oci-apache"))
    req = Request(reference="REQ-2026-0163", requester="mohammed.khader@emaratechg.ae",
                  request_type="create", status="provisioned",
                  deployment_target="oci", environment_name="test")
    req.components = [RequestComponent(technology_code="apache", size="small")]
    s.add(req)
    s.flush()
    s.add(ProvisionedResource(reference="REQ-2026-0163", kind="oci-apache",
                              name="test-req-2026-0163-apache", details={},
                              lifecycle_state="active"))
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def client(db):
    def override():
        yield db
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def orchestrator_returns(monkeypatch, reports, status=200):
    """Stand in for the orchestrator at the TRANSPORT level, so the real
    _orchestrator_boot_reports runs and its output is what gets asserted."""
    captured = {}

    def fake_post(body, signature, path="/provision"):
        captured["path"] = path
        captured["payload"] = json.loads(body)
        if status != 200:
            return None, "unreachable"
        return type("R", (), {
            "status_code": 200,
            "json": lambda self=None: {"reference": "REQ-2026-0163",
                                       "reports": reports},
        })(), None

    monkeypatch.setattr(main, "_post_to_orchestrator", fake_post)
    return captured


# --- The report reaches the person who asked for the machine -----------------

def test_the_machines_own_words_are_returned(client, monkeypatch):
    orchestrator_returns(monkeypatch, [
        {"kind": "oci-apache", "available": True,
         "files": {"REQ-2026-0163-oci-apache.txt": REAL_REPORT}, "note": ""},
    ])
    body = client.get("/api/requests/REQ-2026-0163/boot-report").json()

    assert body["reachable"] is True
    text = body["reports"][0]["files"]["REQ-2026-0163-oci-apache.txt"]
    # Verbatim — not summarised into a verdict. The point is to show what the
    # machine said, not what the portal concluded.
    assert "Oracle Linux Server 9.8" in text
    assert "httpd=active" in text
    assert "http_80=200" in text
    assert "firewall_80=open" in text


def test_it_asks_only_about_this_requests_components(client, monkeypatch):
    captured = orchestrator_returns(monkeypatch, [])
    client.get("/api/requests/REQ-2026-0163/boot-report")
    assert captured["path"] == "/boot-report"
    assert captured["payload"]["reference"] == "REQ-2026-0163"
    assert captured["payload"]["resource_kinds"] == ["oci-apache"]


# --- The credential does not travel ------------------------------------------

def test_the_pre_authenticated_url_never_reaches_the_response(client, monkeypatch):
    """THE property. A PAR is a bearer token in URL form — anyone who saw it
    could read every machine's report forever, without signing in.

    Written as a scan of the whole response rather than a check of one field,
    because the leak that matters is the one nobody thought to look for.
    """
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR_URL)
    orchestrator_returns(monkeypatch, [
        {"kind": "oci-apache", "available": True,
         "files": {"REQ-2026-0163-oci-apache.txt": REAL_REPORT}, "note": ""},
    ])
    raw = client.get("/api/requests/REQ-2026-0163/boot-report").text

    assert "SeCrEtTOkEn123" not in raw
    assert "objectstorage" not in raw
    assert "/p/" not in raw, "something that looks like a PAR path is in the response"


# --- Unreachable is not the same as unreported -------------------------------

def test_an_unreachable_orchestrator_says_so_rather_than_looking_empty(client, monkeypatch):
    """'We could not ask' and 'the machine has not answered' need different
    words: one is a portal problem the user can do nothing about, the other is
    worth waiting for."""
    orchestrator_returns(monkeypatch, [], status=502)
    body = client.get("/api/requests/REQ-2026-0163/boot-report").json()

    assert body["reachable"] is False
    assert body["reports"] == []
    assert "could not be reached" in body["note"]


def test_a_component_that_cannot_report_explains_why(client, monkeypatch):
    """A bucket files no boot report and never will. Saying that is kinder than
    an empty panel, which reads as 'something went wrong'."""
    orchestrator_returns(monkeypatch, [
        {"kind": "oci-bucket", "available": False, "files": {},
         "note": "not a machine — nothing boots, so nothing can report"},
    ])
    body = client.get("/api/requests/REQ-2026-0163/boot-report").json()
    assert body["reachable"] is True
    assert body["reports"][0]["available"] is False
    assert "nothing boots" in body["reports"][0]["note"]


# --- A torn-down environment still has its story -----------------------------

def test_a_decommissioned_request_still_shows_its_report_labelled_historical(
        client, db, monkeypatch):
    """Often exactly what someone wants to look back at: what did that machine
    actually have on it, before we removed it."""
    req = db.get(Request, 1)
    req.status = "decommissioned"
    db.commit()
    orchestrator_returns(monkeypatch, [
        {"kind": "oci-apache", "available": True,
         "files": {"REQ-2026-0163-oci-apache.txt": REAL_REPORT}, "note": ""},
    ])
    body = client.get("/api/requests/REQ-2026-0163/boot-report").json()

    assert body["historical"] is True
    assert body["reports"][0]["available"] is True


def test_a_live_request_is_not_labelled_historical(client, monkeypatch):
    orchestrator_returns(monkeypatch, [])
    assert client.get("/api/requests/REQ-2026-0163/boot-report").json()["historical"] is False


def test_an_unknown_request_is_not_found(client, monkeypatch):
    orchestrator_returns(monkeypatch, [])
    r = client.get("/api/requests/REQ-2026-9999/boot-report")
    assert r.status_code == 404, r.text
