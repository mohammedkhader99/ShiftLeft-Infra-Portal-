"""C12: the ladder fixes its own recipe instead of handing the work to a person.

The reviewer's objection, 2026-08-26, and it is the right one:

    "this is where I want my agent to take over and become intelligent enough
     to take control and fulfil and provision the request. Handing over to my
     human admin team is defeating my shiftleft design and my goal."

The gap was never intelligence. In both failures that mattered, THE AGENT
ALREADY HELD THE ANSWER AND THREW IT AWAY BY REPORTING IT:

    .NET 8    C10 refuses `dotnet8.0` in seconds, with no machine, and names
              `dotnet-sdk-8.0` in the refusal. Nothing consumed it. A person
              read the message and edited the recipe.
    mongodb   the machine reported "could not add the mongodb repository" and
              the URL was a 404. Correct diagnosis, delivered, discarded.

`autobuild.py` has carried a closed loop since C5c — draft, check, prove,
diagnose, redraft — but only for TERRAFORM. The recipe path walked a fixed
ladder of METHODS and never revised the rung it was on. This is that loop,
extended to the half of the system that kept failing.

THE BOUND MATTERS AS MUCH AS THE LOOP. An agent that can revise forever is an
agent that can spend forever. Two revisions, each re-checked before anything is
built, and giving up is a recorded outcome rather than silence.

Nothing here reaches the network or a machine.
"""

from __future__ import annotations

import json

import pytest

from api import autobuild, repo_facts


class _Proposal:
    """The shape `_ensure_vm_service` receives: files, findings, reasoning."""

    def __init__(self, recipe: dict):
        self.files = {"generated/profiles/x.json": json.dumps(recipe)}
        self.findings = []
        self.reasoning = ""
        self.catalogue_rows = []


DOTNET = {"code": "dotnet8", "builds_on": "oci/service-vm", "ports": [5000],
          "version_command": "dotnet --version",
          "rhel": {"packages": ["dotnet8.0"], "services": []}}


def _recipe(proposal) -> dict:
    return json.loads(next(iter(proposal.files.values())))


# --- the mechanical half: carrying an answer into a recipe --------------------

def test_a_package_name_is_replaced_in_the_recipe():
    revised = autobuild._swap_packages(_Proposal(DOTNET),
                                       {"dotnet8.0": "dotnet-sdk-8.0"})

    assert _recipe(revised)["rhel"]["packages"] == ["dotnet-sdk-8.0"]


def test_nothing_to_swap_is_reported_as_nothing():
    """None, not a copy. The caller uses it to decide whether a revision even
    happened, and a copy that changed nothing would loop forever."""
    assert autobuild._swap_packages(_Proposal(DOTNET), {"redis": "redis7"}) is None


def test_the_original_is_not_mutated():
    original = _Proposal(DOTNET)
    autobuild._swap_packages(original, {"dotnet8.0": "dotnet-sdk-8.0"})

    assert _recipe(original)["rhel"]["packages"] == ["dotnet8.0"], (
        "the proposal was edited in place, so a rejected revision would still "
        "have changed the recipe the ladder falls back to")


def test_everything_else_in_the_recipe_survives():
    revised = autobuild._swap_packages(_Proposal(DOTNET),
                                       {"dotnet8.0": "dotnet-sdk-8.0"})
    recipe = _recipe(revised)

    assert recipe["ports"] == [5000]
    assert recipe["version_command"] == "dotnet --version"
    assert recipe["builds_on"] == "oci/service-vm"


# --- the ranked answer, from the ranker that already existed ------------------

def test_the_repository_names_the_package_a_person_would_have_chosen():
    """`discovery.rank_matches` has always known this — its docstring names
    REQ-2026-0197 and `dotnet-sdk-8.0` as the answer. C10 now asks it."""
    offered = {"dotnet-host", "dotnet-sdk-6.0", "dotnet-sdk-7.0", "dotnet-sdk-8.0",
               "dotnet-runtime-8.0", "dotnet-templates-8.0",
               "dotnet-targeting-pack-8.0", "dotnet-sdk-dbg-8.0"}

    near = repo_facts._near("dotnet8.0", offered, "dotnet8")

    assert near, "no suggestion at all"
    assert near[0] == "dotnet-sdk-8.0", f"ranked {near} — the loop would act on {near[0]}"


def test_accessories_are_never_suggested():
    offered = {"dotnet-templates-8.0", "dotnet-targeting-pack-8.0",
               "dotnet-apphost-pack-8.0", "dotnet-sdk-8.0"}

    assert repo_facts._near("dotnet8.0", offered, "dotnet8")[0] == "dotnet-sdk-8.0"


def test_a_refusal_carries_the_names_and_not_only_the_prose():
    """Prose is for people. A refusal that only explains itself to a human is a
    refusal that ends in manual fulfilment."""
    refusal = repo_facts.Refusal("nope", "because", {"a": ("b",)})

    assert refusal.suggestions == {"a": ("b",)}


# --- the loop -----------------------------------------------------------------

def _ladder(objections, **kw):
    """Drive `_ensure_vm_service` with a scripted repository.

    `objections` is consumed one per call, so a test can say "refuse the first
    recipe, accept the second" — which is the whole behaviour under test.
    """
    calls = []

    def ask(recipe):
        calls.append(recipe["rhel"]["packages"][0])
        return objections.pop(0) if objections else None

    result = autobuild._ensure_vm_service(
        "dotnet8", None, _Proposal(DOTNET), target="oci",
        shipped=lambda code: None,               # stop before publishing
        run_proof=None, publish=lambda files: [], certify=None, withdraw=None,
        ask_repository=ask, **kw)
    return result, calls


def test_the_ladder_takes_the_suggestion_instead_of_giving_up():
    """THE test. Before this, the run ended here with a message naming
    `dotnet-sdk-8.0` and a human to read it."""
    refusal = repo_facts.Refusal(
        "This recipe names a package that does not exist.",
        "dotnet8.0 is not published",
        {"dotnet8.0": ("dotnet-sdk-8.0", "dotnet-runtime-8.0")})

    result, tried = _ladder([refusal])

    assert tried == ["dotnet8.0", "dotnet-sdk-8.0"], (
        f"the ladder asked about {tried} — it did not act on the suggestion")
    assert result.status != "refused", (
        "the recipe was corrected and the run was still abandoned")
    assert any(a.stage == "repository" and a.outcome == "revised"
               for a in result.attempts), "the revision was not recorded"


def test_a_recipe_with_no_objection_is_left_alone():
    result, tried = _ladder([])
    assert tried == ["dotnet8.0"]
    assert not any(a.outcome == "revised" for a in result.attempts)


def test_a_refusal_with_no_suggestion_still_refuses():
    """The gate must keep refusing what it cannot fix. Otherwise a technology
    that genuinely is not packaged would spend a machine proving it again."""
    refusal = repo_facts.Refusal("no such package", "nothing like it either", {})

    result, tried = _ladder([refusal])

    assert result.status == "refused"
    assert tried == ["dotnet8.0"]


def test_the_revisions_are_bounded():
    """An agent that can revise forever is an agent that can spend forever."""
    endless = [repo_facts.Refusal("nope", "", {"dotnet8.0": ("a-1",)}),
               repo_facts.Refusal("nope", "", {"a-1": ("a-2",)}),
               repo_facts.Refusal("nope", "", {"a-2": ("a-3",)}),
               repo_facts.Refusal("nope", "", {"a-3": ("a-4",)}),
               repo_facts.Refusal("nope", "", {"a-4": ("a-5",)})]

    result, tried = _ladder(list(endless))

    assert len(tried) <= autobuild.MAX_RECIPE_REVISIONS + 1, (
        f"the ladder revised {len(tried)} times")
    assert result.status == "refused", "giving up must be a recorded outcome"


def test_giving_up_still_says_what_the_repository_said():
    refusal = repo_facts.Refusal("no such package", "dotnet8.0 is not published", {})
    result, _ = _ladder([refusal])

    assert "dotnet8.0 is not published" in result.detail


# --- the same mistake, three times --------------------------------------------

def test_every_refusal_about_a_RECIPE_skips_its_rung_rather_than_ending_the_run():
    """Written after making this mistake three times.

        remembered   the loop always knew this one
        repository   added 2026-08-26, after REQ-2026-0206 stranded mongodb on
                     the package guess
        linted       added 2026-08-27, after REQ-2026-0214 and REQ-2026-0216
                     stranded it AGAIN — the linter correctly refused the
                     vendor-repo recipe, and that refusal ended the whole ladder
                     before the container rung it should have fallen through to

    Each time I fixed the instance in front of me and did not name the class.
    So the class is named now, and this asserts the distinction that decides
    membership: a stage that judges a RECIPE lets the ladder continue; a stage
    that judges the REQUEST ends it.
    """
    recipe_verdicts = {"repository", "linted"}
    request_verdicts = {"catalogue", "preflight", "refused", "published"}

    assert recipe_verdicts <= set(autobuild.PER_RUNG_REFUSALS) | {"remembered"}, (
        "a refusal about one rung's recipe ends the whole run, so a technology "
        "is stranded on whichever rung happened to be refused first")

    assert not (request_verdicts & set(autobuild.PER_RUNG_REFUSALS)), (
        "a verdict about the REQUEST — no egress, over the cost cap, not "
        "installable software at all — is being treated as a skippable rung, so "
        "the ladder will keep spending on something it has already been told to "
        "stop")


def test_remembered_is_not_in_the_set_and_that_is_deliberate():
    """It is a per-rung refusal, but it has its own branch that reads the report
    of the machine that refuted it — that machine may already hold the answer
    the next rung needs. Adding it to the generic set made the skip fire first
    and bypass that work; three tests said so immediately."""
    assert "remembered" not in autobuild.PER_RUNG_REFUSALS
