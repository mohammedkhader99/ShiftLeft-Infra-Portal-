"""Portal help knowledge base (F-INT-08 help layer).

The KB answers "what does X do?" questions, grounded so it can't invent a
feature. Covers topic matching, the offline answer path, the no-match overview,
KB integrity, and the live Claude adapter with the SDK mocked.
"""

import json
import sys
import types

import pytest

from api import portal_help


# --- Topic matching ----------------------------------------------------------

def test_matches_specific_topics():
    assert portal_help.find_topic("what does reduce capacity do?")["key"] == "type-reduce"
    assert portal_help.find_topic("what's the admin page for?")["key"] == "page-admin"
    assert portal_help.find_topic("how does approval work?")["key"] == "gov-approval"
    assert portal_help.find_topic("what is a sandbox request")["key"] == "type-sandbox"


def test_multiword_phrase_beats_incidental_word():
    # "reduce capacity" (2-word phrase) should win over a stray single word.
    topic = portal_help.find_topic("can you reduce capacity on an environment?")
    assert topic["key"] == "type-reduce"


def test_no_match_returns_none():
    assert portal_help.find_topic("what's the weather like") is None
    assert portal_help.find_topic("") is None


# --- Answering (mock) --------------------------------------------------------

def test_answer_returns_the_matched_entry():
    out = portal_help.answer("what does the reduce capacity request do?")
    assert out["mode"] == "mock" and out["matched"] is True
    assert out["title"] == "Reduce capacity request"
    assert "lowers the size" in out["response"]


def test_answer_unmatched_returns_overview():
    out = portal_help.answer("tell me a joke")
    assert out["matched"] is False
    # The overview lists real areas the assistant can explain.
    assert "Request types" in out["response"] and "Admin" in out["response"]


# --- KB integrity ------------------------------------------------------------

def test_every_topic_is_well_formed():
    keys = set()
    for t in portal_help.TOPICS:
        assert t["key"] and t["title"] and t["answer"] and t["keywords"]
        assert t["key"] not in keys, f"duplicate key {t['key']}"
        keys.add(t["key"])
    # Deep coverage: the request types and every nav page are all present.
    assert {"type-reduce", "type-sandbox", "type-temporary", "type-dr", "type-decommission"} <= keys
    assert {"page-new-request", "page-my-requests", "page-overview", "page-showback",
            "page-reports", "page-activity", "page-assistant", "page-admin"} <= keys


# --- Live adapter (Claude SDK mocked) ---------------------------------------

def _inject_fake_anthropic(monkeypatch, text: str, captured: dict | None = None):
    class FakeText:
        type = "text"

        def __init__(self, t):
            self.text = t

    class FakeResp:
        def __init__(self, content):
            self.content = content

    class FakeMessages:
        def create(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            return FakeResp([FakeText(text)])

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", module)


def test_live_answer_is_grounded_in_the_kb(monkeypatch):
    captured: dict = {}
    _inject_fake_anthropic(monkeypatch, "Reduce capacity lowers an environment's size and cost.", captured)
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    out = portal_help.answer("what does reduce capacity do?")
    assert out["mode"] == "live" and out["matched"] is True
    assert "lowers an environment" in out["response"]
    # The whole knowledge base was supplied to the model as its only source.
    sent = captured["messages"][0]["content"]
    assert "Reduce capacity request" in sent and "Portal documentation:" in sent


def test_live_without_key_is_unavailable(monkeypatch):
    _inject_fake_anthropic(monkeypatch, "x")
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(portal_help.AiUnavailable):
        portal_help.answer("what does reduce capacity do?")
