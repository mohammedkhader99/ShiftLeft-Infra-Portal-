"""The supervisor is told what the money is FOR (2026-08-28).

The reviewer's instruction:

    "this additional cost other than a VM should reflect in the user request
     form so when the supervisor receive approval request he should know how
     much cost he is approving in total. Do not put any cost threshold as a
     limit. Let supervisor decide whether to approve the request or disapprove."

Two things, and they pull in the same direction. The ceiling is gone, because a
runner refusing licensed software on an annualised rate was overruling the one
person entitled to decide. And the approval now shows what the total is made of,
because a supervisor cannot exercise that judgement over a single number.

SQL Server is the case: 790.59 monthly, of which 700.00 is LICENCE and 90.59 is
the machine. The requester's own form has always broken that out. The person
granting the money saw one figure.
"""

from __future__ import annotations

import os

import pytest

from common import proof_rules


@pytest.fixture(autouse=True)
def _no_cap(monkeypatch):
    monkeypatch.delenv("CERTIFICATION_COST_CAP_MONTHLY", raising=False)


# --- no ceiling ---------------------------------------------------------------

def test_there_is_no_cost_ceiling_by_default():
    assert proof_rules.cost_cap() is None


def test_an_expensive_plan_is_no_longer_refused():
    """SQL Server: 790.59 monthly, of which 700 is licence, for a proof machine
    that lives about ten minutes and costs roughly 1.80."""
    verdict = proof_rules.check_cost(790.59)

    assert verdict.allowed, verdict.reason
    assert "790.59" in verdict.reason, "the price is no longer recorded anywhere"


def test_the_price_is_still_recorded_even_though_it_gates_nothing():
    """"What did this cost" is a question somebody asks later."""
    assert proof_rules.check_cost(790.59).monthly == 790.59


def test_a_ceiling_can_still_be_set(monkeypatch):
    """Removing the default is not removing the ability. An operator who wants a
    backstop sets one."""
    monkeypatch.setenv("CERTIFICATION_COST_CAP_MONTHLY", "250")

    assert proof_rules.cost_cap() == 250.0
    assert not proof_rules.check_cost(790.59).allowed


def test_an_unpriceable_plan_says_so_rather_than_pretending():
    """With no ceiling the number gates nothing, so refusing on it would refuse
    over an unused figure. It still must not claim a price it does not have."""
    verdict = proof_rules.check_cost(None)

    assert verdict.allowed
    assert "could not be priced" in verdict.reason
    assert verdict.monthly == 0.0


# --- the supervisor sees what the money is for --------------------------------

def _body(**over):
    from api import jira

    estimate = {"currency": "AED",
                "totals": {"one_time": 0.0, "monthly": 790.59, "annual": 9487.04},
                "by_category": {"compute": 86.61, "storage": 3.98, "licence": 700.0,
                                "backup": 0.0, "monitoring": 0.0, "support": 0.0},
                "external_licences": []}
    estimate.update(over)
    return jira, estimate


def test_the_approval_breaks_the_monthly_total_down():
    jira, estimate = _body()
    text = "\n".join(_render(jira, estimate))

    assert "Compute" in text and "86.61" in text
    assert "Licence" in text and "700.00" in text
    assert "Storage" in text and "3.98" in text


def test_the_approval_says_plainly_that_licence_is_not_infrastructure():
    """THE point. An approver reading 790.59 cannot tell a machine from a
    licence commitment, and one of those continues for as long as the
    environment exists."""
    jira, estimate = _body()
    text = "\n".join(_render(jira, estimate))

    assert "SOFTWARE LICENCE" in text
    assert "not" in text.lower() and "infrastructure" in text.lower()


def test_nothing_extra_is_said_when_there_is_no_licence():
    """A plain VM's ticket must not grow a paragraph about licences it has none
    of."""
    jira, estimate = _body(
        by_category={"compute": 86.61, "storage": 3.98, "licence": 0.0,
                     "backup": 0.0, "monitoring": 0.0, "support": 0.0})
    text = "\n".join(_render(jira, estimate))

    assert "SOFTWARE LICENCE" not in text
    assert "Licence" not in text


def test_a_licence_billed_in_another_currency_is_stated_separately():
    """It cannot honestly be added to an AED total, and omitting it silently is
    how somebody approves a cost nobody showed them."""
    jira, estimate = _body(external_licences=[
        {"technology_name": "Some Appliance", "licence_rate": "0.75",
         "licence_currency": "USD"}])
    text = "\n".join(_render(jira, estimate))

    assert "USD" in text and "0.75" in text
    assert "NOT included in the total" in text


def _render(jira, estimate):
    """Call the ticket builder with the smallest request it will accept."""
    class _Comp:
        technology_code = "mssql"
        size = "small"
        vcpu = memory_gb = storage_gb = None

        def __getattr__(self, item):     # version, image, anything else it reads
            return None

    class _Req:
        reference = "REQ-2026-0222"
        components = [_Comp()]
        def __getattr__(self, item):
            return ""

    return jira.build_ticket_body(_Req(), estimate, "no plan").splitlines()
