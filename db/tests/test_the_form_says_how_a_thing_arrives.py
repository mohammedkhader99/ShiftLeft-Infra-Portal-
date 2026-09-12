"""What a blueprint builds must be classified, and it must agree (F-CAT).

THE TEST db/seed.py SAID WAS DOING THIS. Its comment reads:

    Every blueprint now declares whether it delivers a managed service or
    machines, and a test holds that declaration against this table. A technology
    missing from here could not be checked, so a blueprint that started calling
    Kafka a managed service would have passed. Everything any blueprint builds is
    listed, and test_the_form_says_how_a_thing_arrives keeps it that way.

It did not exist. Written 2026-09-12, after a probe blueprint was nearly given a
`builds: [oke-probe]` naming a technology the catalogue has never heard of —
exactly the hole the comment describes, walked into by the person who found the
comment.

TWO HALVES, AND THE FIRST IS WHY THE SECOND WORKS.

*Coverage.* Every technology any blueprint claims to build is in `DELIVERY`. A
technology absent from there has no classification at all, so the agreement check
below has nothing to compare against and passes silently — which is the precise
failure the comment names: "a blueprint that started calling Kafka a managed
service would have passed".

*Agreement.* A blueprint that builds MACHINES may only build things the catalogue
calls a machine or software installed on one; a blueprint that delivers a MANAGED
service may only build things the catalogue calls managed. A capability is an
outcome nobody installs and belongs to neither.

Why it matters beyond tidiness: `delivery` is what tells a requester whether
somebody on their team patches this database at 2am. Two blueprints disagreeing
with the catalogue about that is how "PostgreSQL" came to mean OCI's managed
service in one place and software on a VM in another.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from db.seed import DELIVERY

BLUEPRINTS = pathlib.Path(__file__).resolve().parents[2] / "orchestrator" / "blueprints"

#: What each blueprint-level `delivery` word permits its technologies to be.
#:
#: Anything not named here — a manifest that says nothing, or says a word nobody
#: recognises — is SKIPPED rather than failed. That is deliberate and tested
#: elsewhere: `test_a_manifest_that_does_not_say_is_carried_as_unknown` exists
#: because making `delivery` mandatory once made its technologies unprovisionable
#: and caused a twelve-day outage. This test must not reintroduce that by the
#: back door.
ALLOWED = {
    "vm": {"machine", "software"},
    "managed": {"managed"},
}


def manifests() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(BLUEPRINTS.glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            out.append((path.stem, loaded))
    return out


def built_by_anything() -> list[tuple[str, str]]:
    """(blueprint, technology) for everything any shipped blueprint claims."""
    return [(name, code)
            for name, manifest in manifests()
            for code in (manifest.get("builds") or [])]


def test_there_is_something_to_check():
    """A glob that matches nothing passes every test below silently — the
    failure mode of every scan-based check in this repository."""
    assert len(manifests()) >= 8, [n for n, _ in manifests()]
    assert len(built_by_anything()) >= 10


@pytest.mark.parametrize("blueprint,code", built_by_anything(),
                         ids=lambda v: v if isinstance(v, str) else str(v))
def test_the_form_says_how_a_thing_arrives(blueprint: str, code: str):
    """Every technology a blueprint builds is classified in DELIVERY.

    Without this the agreement test below is blind: an unclassified technology
    has nothing to disagree with, so a blueprint could call anything anything.
    """
    assert code in DELIVERY, (
        f"{blueprint} builds {code!r}, which db.seed.DELIVERY does not classify. "
        f"Either it is a real technology and needs a delivery model, or the "
        f"blueprint is naming something the catalogue has never heard of.")


@pytest.mark.parametrize("blueprint,manifest", manifests(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_a_blueprint_agrees_with_the_catalogue_about_what_it_builds(
        blueprint: str, manifest: dict):
    delivery = (manifest.get("delivery") or "").strip()
    if delivery not in ALLOWED:
        pytest.skip(f"{blueprint} declares delivery={delivery!r}, carried as "
                    f"unknown rather than refused")

    wrong = []
    for code in manifest.get("builds") or []:
        model = (DELIVERY.get(code) or ("",))[0]
        if model and model not in ALLOWED[delivery]:
            wrong.append(f"{code} is {model!r}")

    assert not wrong, (
        f"{blueprint} says delivery={delivery!r}, which builds "
        f"{sorted(ALLOWED[delivery])}, but {'; '.join(wrong)}. `delivery` is what "
        f"tells a requester whether somebody on their team patches this at 2am.")


def test_a_capability_is_never_built_by_anything():
    """An outcome nobody installs has no machine and no endpoint. A blueprint
    claiming to build one would be offering to provision "Backup & Recovery",
    which REQ-2026-0183 spent a real machine learning is not a thing."""
    claimed = [(b, c) for b, c in built_by_anything()
               if (DELIVERY.get(c) or ("",))[0] == "capability"]

    assert not claimed, f"blueprints claiming to build a capability: {claimed}"


def test_the_check_would_catch_a_blueprint_that_lied(tmp_path):
    """THE ONE THAT PROVES THE REST ARE DOING SOMETHING.

    A scan-based test that never sees a failure is indistinguishable from one
    that has stopped looking — the mistake that let `decommission-failed` sit
    unexploded and let a nineteen-character status reach production.
    """
    liar = {"ref": "t/x", "target": "oci", "resource_kind": "t-x",
            "delivery": "managed", "builds": ["kafka"]}

    wrong = [c for c in liar["builds"]
             if (DELIVERY.get(c) or ("",))[0] not in ALLOWED[liar["delivery"]]]

    assert wrong == ["kafka"], (
        "a blueprint calling Apache Kafka a managed service was not caught; "
        "OCI Streaming is the managed alternative and there is no blueprint "
        "for it")
