"""AGENT-DOCTRINE.md is only governance if it arrives in the prompt.

A document nobody reads is a document that stops being true. This suite asserts
the two things that make the doctrine load-bearing rather than decorative:

  * the text a person edits is the text the model receives, and
  * no AI module that shapes a provisioning decision is quietly left out of it.

The second is the same guard the reviewer asked for when a new setting had to
reach the Admin console: adding a new `api/ai_*.py` fails the suite until someone
decides which side of the line it belongs on. Nobody remembers a rule; a red test
asks the question at exactly the moment it matters.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from common import doctrine as D

API = Path(__file__).resolve().parent.parent.parent / "api"


def _source(name: str) -> str:
    return (API / f"{name}.py").read_text(encoding="utf-8")


# --- the document loads at all ------------------------------------------------

def test_the_doctrine_is_present_and_not_empty():
    text = D.doctrine()
    assert len(text) > 2000, "the doctrine is a stub"
    assert "REUSE -> DISCOVER -> COMPOSE -> BUILD" in text, (
        "the decision principle — the one line the whole document exists to "
        "deliver — is missing")


def test_a_missing_doctrine_raises_rather_than_defaulting(tmp_path, monkeypatch):
    """An agent running without its doctrine is not a degraded agent, it is an
    ungoverned one. A silent empty-string fallback is exactly how that ships."""
    monkeypatch.setattr(D, "DOCTRINE_PATH", tmp_path / "gone.md")
    D.doctrine.cache_clear()
    with pytest.raises(D.DoctrineMissing):
        D.doctrine()
    D.doctrine.cache_clear()


def test_a_doctrine_with_no_marked_block_raises(tmp_path, monkeypatch):
    p = tmp_path / "AGENT-DOCTRINE.md"
    p.write_text("# just prose, no markers\n", encoding="utf-8")
    monkeypatch.setattr(D, "DOCTRINE_PATH", p)
    D.doctrine.cache_clear()
    with pytest.raises(D.DoctrineMissing):
        D.doctrine()
    D.doctrine.cache_clear()


# --- the two halves of the document stay apart --------------------------------

def test_the_human_annexes_do_not_reach_the_model():
    """Annex A is a status table written for a person. Feeding the model a list
    of what is NOT BUILT alongside a doctrine describing it as built is how an
    agent ends up narrating a capability that does not exist."""
    text = D.doctrine()
    for annex in ("Annex A", "Annex B", "Annex C", "roadmap, in the order"):
        assert annex not in text, f"{annex!r} leaked into the system prompt"


def test_what_actually_exists_DOES_reach_the_model():
    """The opposite failure. The agent must know which resolution levels are
    real, or it will confidently search a golden-image catalogue we never built.
    This section is inside the markers on purpose."""
    text = D.doctrine()
    assert "Platform reality" in text
    assert "Level 2 never resolves" in text, (
        "the agent is not told the golden-image catalogue is empty")


# --- the doctrine is a stable PREFIX ------------------------------------------

def test_the_doctrine_comes_first():
    """Not cosmetic. An identical prefix across every call is what lets prompt
    caching charge for these tokens once instead of per request."""
    built = D.with_doctrine("Do the thing.")
    assert built.startswith(D.doctrine()), "the task was put in front"
    assert built.rstrip().endswith("Do the thing."), "the task was lost"


def test_the_task_is_still_distinguishable_from_the_standing_rules():
    built = D.with_doctrine("Do the thing.")
    assert "# Your task in this call" in built


# --- every governed module actually carries it --------------------------------

@pytest.mark.parametrize("name", sorted(D.GOVERNED_MODULES))
def test_a_governed_module_sends_the_doctrine(name):
    src = _source(name)
    assert "with_doctrine" in src, f"{name} makes AI calls without the doctrine"


@pytest.mark.parametrize("name", sorted(D.GOVERNED_MODULES))
def test_no_governed_module_sends_a_BARE_system_prompt(name):
    """`api/ai_blueprint.py` — the module that writes Terraform and technology
    profiles, the most authority-adjacent AI call in the platform — ran with NO
    system prompt at all until 2026-08-25. Wiring four of five modules and
    missing the fifth is this project's most repeated defect shape."""
    src = _source(name)
    bare = re.findall(r"system=(?!with_doctrine)\w+", src)
    assert not bare, f"{name} has an ungoverned system prompt: {bare}"


def test_every_ai_module_is_governed_or_deliberately_excluded():
    """The guard that survives the next feature. A new `api/ai_*.py` fails here
    until it is either added to GOVERNED_MODULES or named below with a reason."""
    excluded = {
        # Explains a price already computed server-side. It decides nothing, and
        # two thousand tokens about Terraform layout would make its answers
        # worse, not safer.
        "ai_explainer",
    }
    found = {p.stem for p in API.glob("ai_*.py")}
    unclassified = found - set(D.GOVERNED_MODULES) - excluded
    assert not unclassified, (
        f"new AI module(s) {sorted(unclassified)}: add to "
        f"common.doctrine.GOVERNED_MODULES, or to this test's `excluded` set "
        f"with a reason")


# --- §10: the authority clauses that a test can actually check ----------------

def test_no_ai_module_reads_a_provisioning_CREDENTIAL():
    """Doctrine §10: the agent must not hold or use administrator credentials.

    Asserted as a READ, not as a mention. The first version of this test looked
    for the string `private_key` anywhere in the source and failed immediately —
    on `ai_blueprint`'s own LINTER RULE, the one that refuses generated Terraform
    carrying an inline credential. That is the opposite of the defect: a check
    that cannot tell a guard from the thing it guards against.

    So: what does the module pull out of the environment?"""
    reads = re.compile(
        r"""os\.(?:getenv\(|environ\.get\(|environ\[)\s*["']([A-Z0-9_]+)["']""")
    smells = ("PRIVATE_KEY", "SECRET", "FINGERPRINT", "PASSWORD", "TOKEN",
              "TF_VAR_", "TENANCY", "CREDENTIAL")
    for name in sorted(D.GOVERNED_MODULES):
        for var in reads.findall(_source(name)):
            # ANTHROPIC_API_KEY is the agent's own key. It buys inference, not
            # infrastructure, and holding it is the whole point of the module.
            if var == "ANTHROPIC_API_KEY":
                continue
            assert not any(s in var for s in smells), (
                f"{name} reads {var!r} from the environment — an agent module "
                f"must hold no provisioning credential (doctrine §10)")


def test_no_ai_module_imports_the_orchestrator():
    """Doctrine §10: the agent never triggers execution. The import itself is
    the thing to forbid — code that CAN reach the orchestrator eventually does."""
    for name in sorted(D.GOVERNED_MODULES):
        src = _source(name)
        assert not re.search(r"^\s*(from|import)\s+orchestrator", src, re.M), (
            f"{name} imports the orchestrator (doctrine §10)")


# --- the trap this project has fallen into before -----------------------------

def test_every_image_carrying_common_also_carries_the_doctrine():
    """Passes under pytest, raises in the container.

    `common/doctrine.py` resolves the document relative to the image root. The
    api image copies `api`, `db` and `common` — and nothing else. So the first
    version of this increment was green on 20 tests locally and would have
    raised DoctrineMissing on the first AI call in production.

    This project has met that exact shape before: guarded code that ships inert
    because the container's contents differ from the checkout's."""
    root = Path(__file__).resolve().parent.parent.parent
    checked = 0
    for dockerfile in root.glob("*/Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        if "COPY common" not in text:
            continue
        checked += 1
        assert "COPY AGENT-DOCTRINE.md" in text, (
            f"{dockerfile.parent.name}/Dockerfile copies common/ — so it can "
            f"import common.doctrine — but not the document that module reads")
    assert checked >= 2, f"expected api and orchestrator, inspected {checked}"
