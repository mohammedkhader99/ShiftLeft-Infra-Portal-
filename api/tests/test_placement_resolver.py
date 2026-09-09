"""The resolver offers layouts; the policy decides which are allowed (P.4).

The behaviour worth defending here is not that a forbidden layout is refused —
P.3's Rego tests cover that. It is that a refused layout still comes BACK, with
the sentence explaining it. An option that silently disappears is the failure
this whole step exists to avoid: the requester sees a choice they expected is
missing, cannot tell why, and raises a ticket.

The policy evaluator is injected, so these run without OPA. One test at the end
drives the real Rego rules through a local stand-in built from the same
coresidency.json the server loads, so the two cannot quietly diverge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.placement import (
    CONSOLIDATED_OPTION,
    HOST_MANAGED,
    HOST_VM,
    MANAGED_OPTION,
    OPTION_ORDER,
    SEPARATED_OPTION,
    ComponentFacts,
    enumerate_options,
    resolve_options,
    topology_document,
)

ROOT = Path(__file__).resolve().parents[2]

# The Scenario A selection: a machine, a database and an application runtime.
VM = ComponentFacts(code="compute-vm", host_modes=frozenset({HOST_VM}), is_host=True)
POSTGRES = ComponentFacts(
    code="postgres16",
    host_modes=frozenset({HOST_VM, "container", HOST_MANAGED}),
)
NODEJS = ComponentFacts(code="nodejs20", host_modes=frozenset({HOST_VM, "container"}))
SELECTION = [VM, POSTGRES, NODEJS]


def allow_everything(_topology):
    return {"allow": True, "violations": []}


def refuse_everything(_topology):
    return {"allow": False, "violations": ["nope"]}


# --- the acceptance check ----------------------------------------------------

def test_the_three_options_come_back_in_order():
    options = resolve_options(SELECTION, "dev", "oci", allow_everything)
    assert [o.key for o in options] == list(OPTION_ORDER)
    assert all(o.eligible for o in options)


def test_a_refused_option_is_returned_not_dropped():
    """The point of the whole module."""
    def refuse_consolidated(topology):
        if len(topology["hosts"]) == 1 and len(topology["hosts"][0]["components"]) > 1:
            return {"allow": False,
                    "violations": ["postgres16 may not share a host in prod."]}
        return {"allow": True, "violations": []}

    options = resolve_options(SELECTION, "prod", "oci", refuse_consolidated)

    assert [o.key for o in options] == list(OPTION_ORDER), "still three options"
    consolidated = next(o for o in options if o.key == CONSOLIDATED_OPTION)
    assert not consolidated.eligible
    assert "may not share a host" in consolidated.reasons[0]


def test_a_refusal_always_carries_a_sentence():
    """A policy that refuses without explaining is a defect, and the resolver
    says so rather than rendering a blank 'unavailable'."""
    def refuse_silently(_topology):
        return {"allow": False, "violations": []}

    options = resolve_options(SELECTION, "prod", "oci", refuse_silently)
    for option in options:
        assert not option.eligible
        assert option.reasons and option.reasons[0].strip()
        assert "defect" in option.reasons[0]


def test_a_policy_outage_can_only_refuse_never_allow():
    options = resolve_options(SELECTION, "dev", "oci", refuse_everything)
    assert not any(o.eligible for o in options)


# --- what each option actually lays out --------------------------------------

def test_managed_puts_the_database_on_the_cloud_and_the_rest_on_a_machine():
    """Scenario A option 1: PostgreSQL managed, Node.js on the VM."""
    managed = next(o for o in enumerate_options(SELECTION) if o.key == MANAGED_OPTION)

    by_mode = {h.host_mode: h for h in managed.hosts}
    assert by_mode[HOST_MANAGED].components == ("postgres16",)
    assert by_mode[HOST_VM].components == ("nodejs20",)


def test_consolidated_is_one_machine_carrying_both():
    consolidated = next(
        o for o in enumerate_options(SELECTION) if o.key == CONSOLIDATED_OPTION)
    assert len(consolidated.hosts) == 1
    assert set(consolidated.hosts[0].components) == {"postgres16", "nodejs20"}


def test_separated_is_one_machine_each():
    separated = next(
        o for o in enumerate_options(SELECTION) if o.key == SEPARATED_OPTION)
    assert len(separated.hosts) == 2
    assert all(len(h.components) == 1 for h in separated.hosts)


def test_the_selected_vm_is_a_host_not_a_workload():
    """'VM with OS' is the machine its neighbours land on. Treating it as a
    workload would place a machine on a machine and count it twice."""
    for option in enumerate_options(SELECTION):
        placed = [c for h in option.hosts for c in h.components]
        assert "compute-vm" not in placed


def test_a_managed_service_is_not_counted_as_a_machine():
    """host_count drives sizing and cost. Counting the cloud's database as a
    machine would size and bill for a host nobody provisions."""
    managed = next(o for o in enumerate_options(SELECTION) if o.key == MANAGED_OPTION)
    assert managed.host_count == 1  # the Node.js VM only


# --- options that should not be offered at all -------------------------------

def test_managed_is_not_offered_when_nothing_can_be_managed():
    """Offering a 'managed' option identical to the consolidated one, under a
    name promising less work, would be a lie told twice."""
    keys = [o.key for o in enumerate_options([VM, NODEJS])]
    assert MANAGED_OPTION not in keys


def test_consolidated_is_not_offered_for_a_single_workload():
    """One component consolidated is one component separated."""
    keys = [o.key for o in enumerate_options([VM, NODEJS])]
    assert CONSOLIDATED_OPTION not in keys
    assert SEPARATED_OPTION in keys


def test_a_selection_with_nothing_to_place_yields_no_options():
    assert enumerate_options([VM]) == []


# --- the document handed to the policy ---------------------------------------

def test_the_topology_document_matches_what_the_policy_reads():
    option = enumerate_options(SELECTION)[0]
    doc = topology_document(option, "prod", "oci")

    assert doc["environment"] == "prod"
    assert doc["deployment_target"] == "oci"
    assert all({"id", "host_mode", "components"} <= set(h) for h in doc["hosts"])


# --- against the real rules --------------------------------------------------

def test_the_real_coresidency_facts_refuse_consolidation_in_prod():
    """Drives the resolver with the same coresidency.json the OPA server loads,
    so the resolver and the deployed policy cannot quietly diverge.

    This mirrors the environment rule only; the Rego is the authority and its
    own tests cover the rest. What is checked here is that a real refusal
    reaches the requester as a real sentence.
    """
    facts = json.loads(
        (ROOT / "policy" / "coresidency.json").read_text(encoding="utf-8")
    )["coresidency"]

    def evaluate(topology):
        violations = []
        for host in topology["hosts"]:
            if len(host["components"]) < 2:
                continue
            for component in host["components"]:
                rule = facts.get(component)
                if rule and topology["environment"] in rule["denied_in_environments"]:
                    violations.append(
                        f"{component} may not share a host in "
                        f"{topology['environment']}. {rule['reason']}")
        return {"allow": not violations, "violations": violations}

    in_dev = resolve_options(SELECTION, "dev", "oci", evaluate)
    assert all(o.eligible for o in in_dev)

    in_prod = resolve_options(SELECTION, "prod", "oci", evaluate)
    consolidated = next(o for o in in_prod if o.key == CONSOLIDATED_OPTION)
    assert not consolidated.eligible
    assert "page cache" in consolidated.reasons[0], "the real reason, not a stub"

    # The two that isolate the database remain available, which is the point:
    # the requester is refused one layout, not the request.
    assert next(o for o in in_prod if o.key == MANAGED_OPTION).eligible
    assert next(o for o in in_prod if o.key == SEPARATED_OPTION).eligible


@pytest.mark.parametrize("environment", ["prod", "dr"])
def test_dr_is_refused_exactly_as_prod_is(environment):
    facts = json.loads(
        (ROOT / "policy" / "coresidency.json").read_text(encoding="utf-8")
    )["coresidency"]
    assert environment in facts["postgres16"]["denied_in_environments"]


# --- a cloud service has no machine ------------------------------------------
#
# Added when the catalogue learned that its 17 cloud services are `managed`
# (previously they had no host mode at all). Before that, sizing correctly found
# no requirement row for an object store on a VM and the option came back
# "not priced" — so a layout that CANNOT EXIST was reported in the words used for
# a layout whose numbers are merely missing. One is answered by adding data and
# the other by choosing something else, so they need different sentences.

BUCKET = ComponentFacts(code="oci-objectstorage",
                        host_modes=frozenset({HOST_MANAGED}))
NGINX = ComponentFacts(code="nginx", host_modes=frozenset({HOST_VM}))


def test_consolidating_a_cloud_service_onto_a_machine_is_refused():
    options = {o.key: o for o in enumerate_options([VM, BUCKET, NGINX])}
    consolidated = options[CONSOLIDATED_OPTION]

    assert consolidated.eligible is False
    assert "run by the cloud" in consolidated.reasons[0]
    assert "cannot be installed on a machine" in consolidated.reasons[0]


def test_separating_does_not_give_a_cloud_service_a_machine_either():
    options = {o.key: o for o in enumerate_options([VM, BUCKET, NGINX])}
    assert options[SEPARATED_OPTION].eligible is False


def test_the_refusal_names_the_option_that_does_work():
    """A refusal that does not say what to do instead is half an answer."""
    options = {o.key: o for o in enumerate_options([VM, BUCKET, NGINX])}
    reason = options[CONSOLIDATED_OPTION].reasons[0]

    assert "Managed where available" in reason
    assert MANAGED_OPTION in options, "and it had better be on the screen"


def test_the_cloud_service_is_still_listed_on_the_refused_option():
    """Refused, not quietly emptied. Dropping it and consolidating the rest would
    build an environment missing a component the requester asked for, and the
    option would look like it had worked."""
    options = {o.key: o for o in enumerate_options([VM, BUCKET, NGINX])}
    placed = [c for h in options[CONSOLIDATED_OPTION].hosts for c in h.components]

    assert "oci-objectstorage" in placed
    assert "nginx" in placed


def test_a_component_that_can_run_either_way_is_not_refused():
    """The distinction is "managed and nothing else". PostgreSQL offers a managed
    form AND installs on a machine, so consolidating it is a real choice."""
    both = ComponentFacts(code="postgres16",
                          host_modes=frozenset({HOST_VM, HOST_MANAGED}))
    options = {o.key: o for o in enumerate_options([VM, both, NGINX])}

    assert options[CONSOLIDATED_OPTION].eligible is True


def test_the_managed_option_places_the_cloud_service_off_the_machine():
    options = {o.key: o for o in enumerate_options([VM, BUCKET, NGINX])}
    managed = options[MANAGED_OPTION]

    assert managed.eligible is True
    by_mode = {h.host_mode: h.components for h in managed.hosts}
    assert by_mode[HOST_MANAGED] == ("oci-objectstorage",)
    assert by_mode[HOST_VM] == ("nginx",)


def test_two_cloud_services_are_both_named_in_the_refusal():
    other = ComponentFacts(code="oci-functions",
                           host_modes=frozenset({HOST_MANAGED}))
    options = {o.key: o for o in enumerate_options([VM, BUCKET, other, NGINX])}
    reason = options[CONSOLIDATED_OPTION].reasons[0]

    assert "oci-objectstorage" in reason and "oci-functions" in reason
    assert " are run by the cloud" in reason, "plural, because there are two"
