# Rego unit tests for the placement policy. Run with: opa test /policies
#
# The behaviour that matters most is not that a bad topology is refused — it is
# that the refusal carries a sentence. A policy that returns `false` with no
# reason produces a portal that greys an option out and tells the requester
# nothing, which is the experience these rules exist to avoid.
package infra.placement

import rego.v1

# Two components on one machine: the consolidated option from Scenario A.
consolidated := {
	"environment": "dev",
	"deployment_target": "oci",
	"hosts": [{
		"id": "host-1",
		"host_mode": "vm",
		"components": ["postgres16", "nodejs20"],
	}],
}

# The same two components, one machine each: the separated option.
separated := {
	"environment": "prod",
	"deployment_target": "oci",
	"hosts": [
		{"id": "host-1", "host_mode": "vm", "components": ["postgres16"]},
		{"id": "host-2", "host_mode": "vm", "components": ["nodejs20"]},
	],
}

# PostgreSQL as the cloud's managed service, Node.js on a machine: the
# managed-where-available option, and the one that ought to win in production.
managed := {
	"environment": "prod",
	"deployment_target": "oci",
	"hosts": [
		{"id": "managed-1", "host_mode": "managed", "components": ["postgres16"]},
		{"id": "host-1", "host_mode": "vm", "components": ["nodejs20"]},
	],
}

# --- the acceptance check ----------------------------------------------------

test_consolidation_is_allowed_in_dev if {
	allow with input as consolidated
}

test_the_same_consolidation_is_denied_in_prod if {
	not allow with input as object.union(consolidated, {"environment": "prod"})
}

test_the_denial_names_the_environment_and_gives_a_reason if {
	msgs := violations with input as object.union(consolidated, {"environment": "prod"})
	some msg in msgs
	contains(msg, "postgres16")
	contains(msg, "prod")
	contains(msg, "page cache")
}

test_dr_is_denied_too_because_dr_rehearses_prod if {
	not allow with input as object.union(consolidated, {"environment": "dr"})
}

test_separating_them_is_allowed_in_prod if {
	allow with input as separated
}

test_the_managed_option_is_allowed_in_prod if {
	allow with input as managed
}

# --- pairs that may never share, whatever the tier ---------------------------

test_two_databases_may_not_share_a_host_even_in_dev if {
	not allow with input as {
		"environment": "dev",
		"hosts": [{"id": "h", "host_mode": "vm", "components": ["postgres16", "mysql"]}],
	}
}

test_that_denial_names_both_components if {
	msgs := violations with input as {
		"environment": "dev",
		"hosts": [{"id": "h", "host_mode": "vm", "components": ["postgres16", "mysql"]}],
	}
	some msg in msgs
	contains(msg, "postgres16")
	contains(msg, "mysql")
}

test_a_forbidden_pair_is_reported_once_not_twice if {
	# The facts are symmetric, so walking the pair in both directions emitted the
	# same refusal twice. On the placement screen that is four near-identical
	# paragraphs for two databases in prod, where two say everything.
	msgs := violations with input as {
		"environment": "dev",
		"hosts": [{"id": "h", "host_mode": "vm", "components": ["postgres16", "mysql"]}],
	}
	count([m | some m in msgs; contains(m, "may not share a host.")]) == 1
}

test_the_pair_is_named_in_a_stable_order if {
	msgs := violations with input as {
		"environment": "dev",
		"hosts": [{"id": "h", "host_mode": "vm", "components": ["postgres16", "mysql"]}],
	}
	some msg in msgs
	startswith(msg, "mysql and postgres16 may not share a host.")
}

test_the_order_components_are_listed_in_does_not_change_the_answer if {
	# Sorting the pair must not make the refusal depend on how the resolver
	# happened to order the components it placed.
	reversed := violations with input as {
		"environment": "dev",
		"hosts": [{"id": "h", "host_mode": "vm", "components": ["mysql", "postgres16"]}],
	}
	forward := violations with input as {
		"environment": "dev",
		"hosts": [{"id": "h", "host_mode": "vm", "components": ["postgres16", "mysql"]}],
	}
	reversed == forward
}

test_an_asymmetric_fact_is_still_caught if {
	# Only one side names the other. Checking a single direction would let this
	# through whenever the unlisted name sorted lower.
	msgs := violations with input as {
		"environment": "dev",
		"hosts": [{"id": "h", "host_mode": "vm", "components": ["aaa-engine", "zzz-engine"]}],
	}
		with data.coresidency as {"zzz-engine": {
			"denied_in_environments": [],
			"denied_with": ["aaa-engine"],
			"reason": "Two engines on one machine compete for the same memory.",
		}}
	some msg in msgs
	msg == "aaa-engine and zzz-engine may not share a host. Two engines on one machine compete for the same memory."
}

test_a_component_with_no_rule_is_placeable if {
	# A component nobody has written a co-residency rule for must not become
	# unrequestable the day it is added to the catalogue.
	allow with input as {
		"environment": "prod",
		"hosts": [{"id": "h", "host_mode": "vm", "components": ["nodejs20", "nginx"]}],
	}
}

# --- topologies that describe something unbuildable --------------------------

test_a_managed_service_cannot_host_other_components if {
	not allow with input as {
		"environment": "dev",
		"hosts": [{
			"id": "m",
			"host_mode": "managed",
			"components": ["postgres16", "nodejs20"],
		}],
	}
}

test_an_empty_host_is_refused if {
	not allow with input as {
		"environment": "dev",
		"hosts": [{"id": "ghost", "host_mode": "vm", "components": []}],
	}
}

test_the_empty_host_refusal_names_it if {
	msgs := violations with input as {
		"environment": "dev",
		"hosts": [{"id": "ghost", "host_mode": "vm", "components": []}],
	}
	some msg in msgs
	contains(msg, "ghost")
}

test_a_placement_with_no_hosts_is_refused if {
	not allow with input as {"environment": "dev", "hosts": []}
}

# --- every refusal must be explicable ----------------------------------------

test_nothing_is_refused_without_a_reason if {
	# The rule this whole file is built around: if it is not allowed, there is
	# at least one sentence saying why.
	denied := object.union(consolidated, {"environment": "prod"})
	not allow with input as denied
	count(violations) > 0 with input as denied
}

test_an_allowed_topology_has_no_violations if {
	count(violations) == 0 with input as separated
}

# --- the failure that would disable this policy without failing anything -----

test_the_coresidency_facts_are_loaded if {
	# If coresidency.json stops loading, `coresidency` falls back to {}, every
	# rule above becomes vacuous, and the policy answers `allow` for topologies
	# it is supposed to refuse — while every other test here still passes,
	# because they would all be asserting on an empty ruleset.
	count(coresidency) > 0
	"postgres16" in object.keys(coresidency)
	"prod" in coresidency.postgres16.denied_in_environments
}
