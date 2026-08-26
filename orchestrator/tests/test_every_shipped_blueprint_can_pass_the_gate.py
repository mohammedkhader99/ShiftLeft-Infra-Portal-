"""Can everything already in the catalogue pass the gate we switched on?

Nobody asked that question when the certification gate shipped, and the answer
was no. For about twelve days a bare VM could not be provisioned at all:
`compute-vm` and `rhel9` install nothing by design, the boot script reported
that as `PORTAL FAILURE: ... has no install recipe`, and the verdict failed a
machine that had booted perfectly. REQ-2026-0208 spent a real machine to
rediscover it, and every bare VM request before it would have too.

The reviewer found it by asking for a VM and being told no. That is the wrong
way to find this, so it is a test now.

WHAT THIS GUARDS, in one sentence: a gate may not be enabled for a catalogue
that cannot pass it. Every technology a shipped blueprint builds must be able
to produce a report the verdict accepts — either because it has an install
recipe, or because the catalogue says it IS the machine and installs nothing.

A blueprint that renders its OWN cloud-init (`boot_report: template`) or files
no report (`boot_report: none`) is out of scope here, and saying so explicitly
matters: the first version of this survey reported Kafka and OKE as broken
because it did not check which mechanism carried the report.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import boot_reports, configure

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINTS = sorted(glob.glob(str(ROOT / "blueprints" / "*.yaml")))

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)


def _delivery_models() -> dict[str, str]:
    """What the seeded catalogue says about each technology."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from db.models import TechnologyDelivery
    from db.seed import seed
    from db.session import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        seed(s)
        s.commit()
        return {r.technology_code: (r.delivery_model or "")
                for r in s.scalars(select(TechnologyDelivery))}


def _cases():
    """(blueprint ref, technology, os family) for everything configure.py renders."""
    models = _delivery_models()
    for path in BLUEPRINTS:
        bp = yaml.safe_load(open(path, encoding="utf-8"))
        if bp.get("boot_report") != "user_data":
            continue                     # its own cloud-init, or no report
        cannot = bp.get("cannot_verify") or {}
        for code in bp.get("builds") or []:
            if code in cannot:
                continue                 # declared unverifiable, with a reason
            for family in bp.get("os_families") or ["rhel"]:
                yield bp.get("ref", os.path.basename(path)), code, family, models


CASES = list(_cases())


def test_the_survey_actually_covers_something():
    """A survey that inspects nothing passes trivially and proves nothing."""
    assert len(CASES) >= 6, f"only {len(CASES)} blueprint/technology/os combinations"


@pytest.mark.parametrize("ref,code,family,models",
                         CASES, ids=[f"{r}:{c}:{f}" for r, c, f, _ in CASES])
def test_a_shipped_blueprint_can_produce_a_report_the_gate_accepts(
        ref, code, family, models):
    delivers = models.get(code, "")
    rendered = configure.render(
        [{"technology_code": code, "delivers": delivers}], family,
        configure.boot_report_url("PROOF-SURVEY", "oci-instance"))

    failures = [l for l in rendered.splitlines() if "PORTAL FAILURE" in l]
    unprovable = [l for l in failures if "has no install recipe" in l]

    assert not unprovable, (
        f"{ref} builds {code} on {family}, and the machine would report "
        f"{unprovable[0].strip()[:90]!r}. The verdict reads that as a failure, "
        f"so this technology can never be certified and every request for it "
        f"ends in manual fulfilment — after spending a real machine to find "
        f"out. Either give it an install recipe, or record it in the catalogue "
        f"as delivery_model='machine' if it IS the machine "
        f"(it is currently {delivers or 'unclassified'!r}).")


def test_a_machine_delivery_technology_reports_success_not_failure():
    """The fix, stated directly. REQ-2026-0208's machine booted Oracle Linux
    9.8, ran cloud-init and filed its report — and was failed for containing
    exactly what a bare VM should contain."""
    rendered = configure.render(
        [{"technology_code": "compute-vm", "delivers": "machine"}], "rhel",
        configure.boot_report_url("PROOF-SURVEY", "oci-instance"))

    assert "PORTAL FAILURE: compute-vm" not in rendered
    assert "is a machine; there is nothing to install" in rendered


def test_the_success_wording_survives_the_verdict():
    """`boot_reports.verdict` condemns a PORTAL line carrying "failed", "could
    not" or "no install recipe". A success phrased carelessly would still fail,
    and the machine would be just as dead for a reason nobody could see."""
    report = ("os_family=rhel\ntechnologies=\n--- first-boot log ---\n"
              "PORTAL: compute-vm is a machine; there is nothing to install on it\n"
              "PORTAL: first-boot configuration finished\n")

    assert boot_reports.verdict(report)["ok"], boot_reports.verdict(report)


def test_software_with_a_MISSING_recipe_still_fails():
    """The other half, and the reason this is read from the catalogue rather
    than inferred from the absence of a recipe: a recipe missing by mistake must
    keep looking like the failure it is."""
    rendered = configure.render(
        [{"technology_code": "nginx", "delivers": "software"}], "suse",
        configure.boot_report_url("PROOF-SURVEY", "oci-instance"))

    assert "PORTAL FAILURE" in rendered or "nginx" not in rendered, (
        "software with no recipe for this family reported success")


def test_an_unclassified_technology_is_treated_as_software():
    """Unclassified is not a licence to assume 'machine'. Guessing that way
    would make a broken recipe pass silently — the exact thing the gate exists
    to catch."""
    rendered = configure.render(
        [{"technology_code": "made-up-thing"}], "rhel",
        configure.boot_report_url("PROOF-SURVEY", "oci-instance"))

    assert "PORTAL FAILURE: made-up-thing has no install recipe" in rendered
