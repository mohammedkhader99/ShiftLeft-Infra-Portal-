"""A name the module accepts can still be a name the cloud refuses.

REQ-2026-0246 asked for OKE. The portal composed a name the module's own
validation accepted, Terraform planned it cleanly, and twenty minutes into the
apply — after the VCN, the subnets and the cluster existed — OCI said:

    400-InvalidParameter, Invalid name:
    Node pool name must be 32 characters or less.

Because the module does not use `instance_name` only for the thing it names. It
DERIVES other names from it:

    name = "${local.label_prefix}-node-pool"      instance_name + 10
    32 - 10 = 22, and the manifest declared 24.

THE SECOND TIME A DERIVED NAME HAS BROKEN THIS BLUEPRINT. The first was fixed by
declaring the limit of the resource that is named DIRECTLY — which is what
`name_max_length` has meant ever since. The node pool is named INDIRECTLY, has
its own tighter cap, and nothing was checking it.

A PLAN failure is free. This one failed at APPLY, with real resources already
created and a teardown that had to succeed for nothing to be left billing.

So this reads the suffixes out of the module itself rather than from a list kept
beside it, and holds them against the number in the manifest. Raise the limit,
or rename a suffix longer, and this fails instead of a real build failing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

MODULES = Path("orchestrator/terraform/oci")
BLUEPRINTS = Path("orchestrator/blueprints")

#: What OCI actually caps each derived name at.
#:
#: Taken from the API's OWN REFUSAL, not from documentation that may have moved:
#: "Node pool name must be 32 characters or less" is the message that failed
#: PROOF-OCI-OKE-20260831T051051. A suffix absent from here is not checked —
#: display names are capped at 255 and nothing here comes close — so this lists
#: the ones known to be tight, and says so rather than pretending to be complete.
CAPS = {"-node-pool": 32}

#: Where a module's derived names come from. `label_prefix` is the module's own
#: alias for the name the portal supplies.
DERIVED = re.compile(r"\$\{local\.label_prefix\}(-[a-z0-9-]+)")


def manifests():
    for path in sorted(BLUEPRINTS.glob("*.yaml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        module = (manifest.get("module") or ".").strip()
        if module in ("", "."):
            continue
        directory = Path("orchestrator/terraform") / module
        if directory.is_dir():
            yield path.name, manifest, directory


def suffixes(directory: Path) -> set[str]:
    found: set[str] = set()
    for tf in directory.rglob("*.tf"):
        found |= set(DERIVED.findall(tf.read_text(encoding="utf-8")))
    return found


def test_there_are_modules_to_check():
    """A glob that matches nothing passes every test below in silence."""
    checked = list(manifests())

    assert checked, "no blueprint resolves to a module directory"
    assert any(name == "oci-oke.yaml" for name, _, _ in checked)


def test_the_oke_module_still_derives_the_name_this_is_about():
    """If the node pool stops being named this way the arithmetic below is
    vacuous, and would pass while protecting nothing."""
    directory = Path("orchestrator/terraform/oci/oke")

    assert "-node-pool" in suffixes(directory), (
        "the OKE module no longer derives its node pool name from the instance "
        "name; the cap below needs rechecking against whatever it does now")


@pytest.mark.parametrize(
    "name,manifest,directory", list(manifests()),
    ids=[name for name, _, _ in manifests()])
def test_a_derived_name_still_fits(name, manifest, directory):
    """THE GUARD. Every name the module builds from the one the portal supplies
    must fit the cloud's cap for that kind of name."""
    limit = int(manifest.get("name_max_length") or 0)
    if not limit:
        return  # states no limit; nothing to hold it to

    for suffix in sorted(suffixes(directory)):
        cap = CAPS.get(suffix)
        if cap is None:
            continue
        assert limit + len(suffix) <= cap, (
            f"{name} declares name_max_length {limit}, and this module names a "
            f"resource '<name>{suffix}' which the cloud caps at {cap}. "
            f"{limit} + {len(suffix)} = {limit + len(suffix)}. The build will "
            f"pass its plan and be refused at APPLY, with real resources "
            f"already created. Declare at most {cap - len(suffix)}.")
