# METADATA
# title: Placement policy
# description: Whether a proposed host topology may be built. Judges what shares a machine with what, and where sharing is forbidden outright. Every refusal carries the sentence shown to the requester.
# scope: package
# related_resources:
#   - https://github.com/emaratech/infra-portal/blob/main/ARCHITECTURE.md
package infra.placement

# Placement policy for the resolver (P.3, F-GOV-12).
#
# Evaluated by OPA against a CANDIDATE topology — not against a request. The
# resolver enumerates options and asks this package about each one, so a denied
# option can be shown to the requester with the reason beside it rather than
# quietly dropped. Silent filtering makes the portal feel broken and generates
# the tickets the portal exists to prevent.
#
# The rules are here and the facts are in coresidency.json, so adding a
# component is a data change, not a code change (ARCHITECTURE.md P5). The policy
# folder is mounted read-only, which means a co-residency change is reviewed and
# versioned like any other change to what the platform will allow — appropriate
# for a rule whose whole job is to refuse things.
#
# Returns `allow` plus human-readable `violations`, the same shape infra.authz
# returns, so the admin console's Policies page renders these with no new code
# and can never describe a rule that is not the one enforcing.

import rego.v1

default allow := false

allow if count(violations) == 0

# The co-residency facts, from coresidency.json in this folder.
#
# Addressed by its exact path rather than looked up in `data`: reaching for the
# whole document makes this rule depend on every rule in it, including the ones
# below that depend on this — a recursion the compiler refuses outright.
#
# The default matters more than it looks. If coresidency.json ever failed to
# load, an undefined value here would make every co-residency rule below
# undefined too, and the policy would permit everything while still answering
# `allow` — a governance gate that has quietly stopped governing.
# `test_the_coresidency_facts_are_loaded` exists to catch exactly that.
default coresidency := {}

coresidency := data.coresidency

rule_for(component) := object.get(coresidency, component, {
	"denied_in_environments": [],
	"denied_with": [],
	"reason": "",
})

# A host carrying more than one component is a consolidation decision, and only
# consolidation can breach a co-residency rule.
shared_hosts contains host if {
	some host in input.hosts
	count(host.components) > 1
}

# METADATA
# title: Co-residency denied in this environment
# description: Some components may not share a machine at all in the stated environment tiers — typically databases in production and DR, where a neighbour competing for the page cache or a restart to patch it takes the database down with it.
# custom:
#   feature: F-GOV-12
#   effect: block
#   applies_to: consolidated topologies in a denied environment
violations contains msg if {
	some host in shared_hosts
	some component in host.components
	rule := rule_for(component)
	input.environment in rule.denied_in_environments
	msg := sprintf(
		"%v may not share a host in %v. %v",
		[component, input.environment, rule.reason],
	)
}

# A pair is forbidden if EITHER component names the other.
#
# Checked in both directions on purpose: the facts are written by hand, and one
# side of a pair being missed is a likely mistake. Refusing only when the pair
# happens to be listed on the side the iteration reached first would make a
# governance rule depend on which name sorts lower.
forbidden_pair(one, other) if other in rule_for(one).denied_with

forbidden_pair(one, other) if one in rule_for(other).denied_with

# Whichever side actually carries the rule supplies the sentence, so an
# asymmetric fact still explains itself.
pair_reason(one, other) := rule_for(one).reason if {
	other in rule_for(one).denied_with
}

pair_reason(one, other) := rule_for(other).reason if {
	not other in rule_for(one).denied_with
	one in rule_for(other).denied_with
}

# METADATA
# title: Components that may not share a host with each other
# description: Some pairs must never be co-resident in any environment, whatever the tier — two database engines on one machine competing for the same memory, or a licensing boundary that a shared host would cross.
# custom:
#   feature: F-GOV-12
#   effect: block
#   applies_to: any topology placing both components on one host
violations contains msg if {
	some host in input.hosts
	some first in host.components
	some second in host.components

	# ONE ORDERING, NOT BOTH. `first != second` walked the pair twice and, since
	# these facts are written symmetrically, emitted the same refusal in both
	# directions: a requester consolidating two databases in prod was shown four
	# near-identical paragraphs where two say everything. Sorting the pair reports
	# it once, and forbidden_pair above keeps the check itself two-directional.
	first < second
	forbidden_pair(first, second)
	msg := sprintf(
		"%v and %v may not share a host. %v",
		[first, second, pair_reason(first, second)],
	)
}

# METADATA
# title: A managed service has no host
# description: A managed service is run by the cloud, so placing it on a machine the requester owns describes something that cannot be built — and would silently add that machine's cost and capacity to the estimate.
# custom:
#   feature: F-CAT-17
#   effect: block
#   applies_to: topologies placing a managed component on a host
violations contains msg if {
	some host in input.hosts
	host.host_mode == "managed"
	count(host.components) > 1
	msg := sprintf(
		"A managed service is run by the cloud and cannot host %v other component(s).",
		[count(host.components) - 1],
	)
}

# METADATA
# title: A host must carry something
# description: An empty host is a machine nobody asked for that the request would nonetheless provision and bill.
# custom:
#   feature: F-GOV-12
#   effect: block
#   applies_to: all topologies
violations contains msg if {
	some host in input.hosts
	count(host.components) == 0
	msg := sprintf("Host '%v' carries no components; it would be built and billed for nothing.", [object.get(host, "id", "?")])
}

# METADATA
# title: A topology must place something
# description: A placement that places nothing is not a placement. Reaching costing with an empty topology produces an estimate of zero for a request that does provision resources.
# custom:
#   feature: F-GOV-12
#   effect: block
#   applies_to: all topologies
violations contains msg if {
	count(input.hosts) == 0
	msg := "This placement has no hosts; nothing would be provisioned."
}
