"""AI natural-language layer for the approvals chatbot (F-INT-08, AI Assistant).

Turns a plain-English message into EXACTLY ONE of the approvals bot's safe
commands (pending / status / approve / reject / help). It only *interprets* — the
existing authority-preserving engine (`api/chatbot.py`) still executes every
command, so Jira stays the system of record and RBAC + segregation of duties are
unchanged.

Safety boundary (mirrors the whole project's P3 rule — the AI recommends, it
never decides or executes):

* Read-only intents (pending / status / help) are safe to run immediately.
* A state-changing intent (approve / reject) is **never executed here**. It comes
  back as a *proposed command* for a human to confirm; only the confirm click
  runs the real `/api/chatops` path. The AI's interpretation can never, by
  itself, record a decision in Jira.

Mock-first (a deterministic offline parser, always works and costs nothing) with
a live Claude adapter behind AI_MODE=live — the same pattern as the other AI
features, reusing `ai_drafter`'s shared client/model plumbing.
"""

from __future__ import annotations

import json
import re

# Reuse the drafter's shared AI plumbing so every AI feature behaves identically.
from api.ai_drafter import AiUnavailable, ai_mode, ai_model, anthropic_client
from common.doctrine import with_doctrine

# The commands the bot understands. 'explain' = a question about what a portal
# page/feature does (answered from the help knowledge base); 'unknown' = no match.
ACTIONS = ("pending", "status", "approve", "reject", "help", "explain", "unknown")
READONLY_ACTIONS = ("pending", "status", "help")
DECIDING_ACTIONS = ("approve", "reject")

# Request references look like REQ-2026-0001 (REQ-<year>-<zero-padded-seq>).
_REF_RE = re.compile(r"\breq[-\s]?(\d{4})[-\s]?(\d{1,6})\b", re.IGNORECASE)


def _normalize_reference(value: str | None) -> str | None:
    """Pull a request reference out of free text and standardise it to
    REQ-YYYY-NNNN, or None if there isn't one."""
    if not value:
        return None
    m = _REF_RE.search(value)
    if not m:
        return None
    return f"REQ-{m.group(1)}-{int(m.group(2)):04d}"


# --- Mock interpreter (deterministic keyword parsing) ------------------------

def _extract_note(message: str, reference: str | None) -> str:
    """For approve/reject, treat whatever follows the reference as the reason."""
    if not reference:
        return ""
    m = _REF_RE.search(message or "")
    if not m:
        return ""
    return message[m.end():].strip(" .,:;-—\t\n")


def _interpret_mock(message: str) -> dict:
    text = (message or "").lower()
    reference = _normalize_reference(message)

    def has(*phrases: str) -> bool:
        return any(p in text for p in phrases)

    # Order matters: reject is checked before approve so "do not approve" wins;
    # commands before the reference fallback; a portal question (no reference)
    # becomes 'explain'.
    if has("help", "commands", "what can you do", "how do i", "how do you"):
        action = "help"
    elif has("reject", "decline", "deny", "refuse", "turn down", "do not approve", "don't approve"):
        action = "reject"
    elif has("approve", "sign off", "sign-off", "looks good", "ok to go", "go ahead",
             "green light", "lgtm", "good to go"):
        action = "approve"
    elif has("pending", "waiting", "queue", "awaiting", "to approve", "need my",
             "on me", "for me", "my approval"):
        action = "pending"
    elif reference:  # a bare reference, or "status/show REQ-x" → show its status
        action = "status"
    elif has("what", "how", "why", "purpose", "explain", "difference", "mean",
             "used for", "tell me", "describe", "does", " do "):
        action = "explain"  # a question about the portal → answered from the help KB
    else:
        action = "unknown"

    note = _extract_note(message, reference) if action in DECIDING_ACTIONS else ""
    return {"action": action, "reference": reference, "note": note}


# --- Live interpreter (Claude, schema-constrained) ---------------------------

_SYSTEM = (
    "You are the natural-language front-end to an infrastructure provisioning "
    "approvals bot. Read the user's message and map it to EXACTLY ONE command: "
    "'pending' (list requests awaiting approval), 'status' (one request's "
    "status), 'approve', 'reject', 'help' (what commands exist), or 'explain' "
    "(the user is asking what a portal page or feature does or means). Use "
    "'unknown' if it matches none. Set 'reference' to the request id when the "
    "message names one (format REQ-YYYY-NNNN), else an empty string. For approve/"
    "reject, copy any short reason the user gives into 'note'. You ONLY interpret "
    "— you never approve, reject, or act, and a human confirms every approve/"
    "reject before it runs."
)


def _live_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "reference", "note"],
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "reference": {"type": "string"},
            "note": {"type": "string"},
        },
    }


def _interpret_live(message: str) -> dict:
    """Ask Claude to map the message to a command. Raises AiUnavailable (never
    crashes the request) on any SDK/key/API problem."""
    client = anthropic_client()
    try:
        resp = client.messages.create(
            model=ai_model(),
            max_tokens=500,
            system=with_doctrine(_SYSTEM),
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _live_schema()},
            },
            messages=[{"role": "user", "content": (message or "").strip()}],
        )
    except Exception as exc:  # noqa: BLE001 — surface any SDK/transport/API error cleanly
        raise AiUnavailable(f"The AI service call failed: {exc}") from exc

    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if not text:
        raise AiUnavailable("The AI service returned an empty result.")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AiUnavailable("The AI service returned an unreadable result.") from exc
    return {
        "action": parsed.get("action") or "unknown",
        "reference": parsed.get("reference") or None,
        "note": (parsed.get("note") or "").strip(),
    }


# --- Safety-net constraint (runs on mock AND live output) --------------------

def _constrain(raw: dict) -> dict:
    """Force the interpretation into the known command set and normalise the
    reference. A decide (approve/reject) with no valid reference is downgraded to
    'unknown' — the bot must never propose a decision without a target."""
    action = (raw.get("action") or "unknown").strip().lower()
    if action not in ACTIONS:
        action = "unknown"
    reference = _normalize_reference(raw.get("reference"))
    note = (raw.get("note") or "").strip()
    warnings: list[str] = []

    if action in ("status",) + DECIDING_ACTIONS and not reference:
        warnings.append(f"Couldn't find a request id (like REQ-2026-0001) to {action}.")
        if action in DECIDING_ACTIONS:
            action = "unknown"  # can't propose a decision without a target

    return {
        "action": action,
        "reference": reference,
        "note": note if action in DECIDING_ACTIONS else "",
        "warnings": warnings,
    }


def interpret(message: str) -> dict:
    """Interpret a plain-English message into a bot command.

    Returns {mode, action, reference, note, warnings}. This is interpretation
    ONLY — it runs no command and records nothing. The endpoint executes read-only
    intents via the existing engine and returns approve/reject as a proposal for a
    human to confirm.
    """
    if ai_mode() == "live":
        raw = _interpret_live(message)
        mode = "live"
    else:
        raw = _interpret_mock(message)
        mode = "mock"
    out = _constrain(raw)
    out["mode"] = mode
    return out
