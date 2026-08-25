"""The agent's operating doctrine, as the literal system prompt.

`AGENT-DOCTRINE.md` is the fourth governing document (adopted 2026-08-25). Half
of it is written for people — a status annex and a roadmap. The other half is
written for the model, and this module is what puts it there.

Why load it from the markdown rather than keeping the text in Python: a doctrine
that lives in two places diverges, and this project has paid for that shape six
times — most recently two functions answering one reachability question, which
disagreed three times before one of them was deleted. The document a person edits
and the prompt the agent receives must be the same characters.

The doctrine goes FIRST in every system prompt. That is not cosmetic: an
identical prefix across calls is what makes prompt caching work, so the ~2,000
tokens are paid for once rather than per request.
"""

from __future__ import annotations

import functools
from pathlib import Path

DOCTRINE_PATH = Path(__file__).resolve().parent.parent / "AGENT-DOCTRINE.md"

_BEGIN = "<!-- DOCTRINE-BEGIN -->"
_END = "<!-- DOCTRINE-END -->"

#: The AI modules whose calls carry the doctrine. Every module that makes, or
#: materially shapes, a provisioning decision belongs here.
#:
#: `ai_explainer` is deliberately absent: it explains a price that has already
#: been computed server-side and decides nothing. Handing it two thousand tokens
#: about Terraform module layout would make its answers worse, not safer.
#:
#: This set is enforced by a test, so adding a new `api/ai_*.py` fails the suite
#: until someone decides which side of the line it is on. That is the same guard
#: the reviewer asked for when a new setting had to reach the Admin console.
GOVERNED_MODULES = frozenset({
    "ai_blueprint",   # writes Terraform and technology profiles
    "ai_chat",        # interprets approve/reject on the approvals bot
    "ai_drafter",     # drafts requests
    "ai_recommend",   # recommends cloud, stack and sizing
    "ai_triage",      # diagnoses a failed build and proposes remediation
})


class DoctrineMissing(RuntimeError):
    """The doctrine could not be loaded.

    Raised rather than defaulted. An agent running without its doctrine is not a
    degraded agent, it is an ungoverned one, and a silent empty-string fallback
    is exactly how that would ship unnoticed.
    """


@functools.lru_cache(maxsize=1)
def doctrine() -> str:
    """The system-prompt half of AGENT-DOCTRINE.md, verbatim."""
    try:
        text = DOCTRINE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise DoctrineMissing(f"cannot read {DOCTRINE_PATH}: {exc}") from exc

    start = text.find(_BEGIN)
    end = text.find(_END)
    if start < 0 or end < 0 or end <= start:
        raise DoctrineMissing(
            f"{DOCTRINE_PATH.name} has no {_BEGIN} ... {_END} block")

    body = text[start + len(_BEGIN):end].strip()
    if not body:
        raise DoctrineMissing(f"{DOCTRINE_PATH.name} carries an empty doctrine")
    return body


def with_doctrine(task: str) -> str:
    """Prepend the doctrine to a call's own instructions.

    The task keeps its own heading so the model can tell the standing rules from
    what it is being asked to do right now.
    """
    return f"{doctrine()}\n\n---\n\n# Your task in this call\n\n{task.strip()}"
