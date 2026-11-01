"""The SIX STATES of Meta advertising spend, and the rule that names them.

The rule itself is pure, so this test does not need a site, a bench, or a
Frappe connection. That is deliberate: the rule decides which bucket a piece
of advertising money belongs in, and a rule that can only be exercised against
a live instance is a rule that is rarely exercised.

The rules this holds:

1. There are exactly SIX states, in the brief's order.
2. The reason vocabulary is CLOSED. A reason that is not in it is reported as
   the generic one, never passed through to a screen.
3. The precedence is the brief's: already invoiced, then ambiguous, then
   switched off, then unassigned, then blocked, then assigned and billable.
4. "Switched off" and "unassigned" are NEVER the same answer — that is the
   whole point of the sixth state the brief asks for.
5. An unknown attribution status is reported as unrecognised, not as "no page
   mapping", because the two send a reader to different places.
"""

from __future__ import annotations

import pytest

from laratik_cashbox import meta_ads_chargeability as C


def evidence(**over):
    record = {
        "attribution_status": "Attributed",
        "switched_off": False,
        "invoice_source": "",
        "blocked_reason": "",
    }
    record.update(over)
    return record


def test_there_are_six_states_in_the_brief_s_order():
    assert list(C.CHARGEABILITY_STATES) == [
        "assigned_billable",
        "assigned_not_billable",
        "unassigned",
        "ambiguous",
        "already_invoiced",
        "blocked",
    ]


def test_every_state_has_at_least_one_reason_and_every_reason_has_a_state():
    for state in C.CHARGEABILITY_STATES:
        assert C.CHARGEABILITY_REASONS.get(state), f"{state} has no reason"

    known = {reason for reasons in C.CHARGEABILITY_REASONS.values() for reason in reasons}
    for state, reasons in C.CHARGEABILITY_REASONS.items():
        for reason in reasons:
            assert reason in known
            assert reason


def test_the_ordinary_case_is_assigned_and_billable():
    assert C.classify_chargeability(evidence()) == (
        "assigned_billable",
        "matched_page_and_profile",
    )


def test_a_switched_off_arrangement_is_ASSIGNED_BUT_NOT_BILLABLE():
    # This is the pair the brief singles out. A switched-off arrangement is a
    # DECISION somebody made; an unmapped Page is a GAP nobody has filled.
    state, reason = C.classify_chargeability(
        evidence(attribution_status="Unmapped", switched_off=True)
    )

    assert state == "assigned_not_billable"
    assert reason == "profile_switched_off"


def test_an_unmapped_page_is_unassigned_and_NOT_not_billable():
    state, reason = C.classify_chargeability(evidence(attribution_status="Unmapped"))

    assert state == "unassigned"
    assert reason == "no_page_mapping"


def test_an_ambiguous_page_is_ambiguous():
    state, reason = C.classify_chargeability(evidence(attribution_status="Ambiguous"))

    assert state == "ambiguous"
    assert reason == "ambiguous_page_identity"


def test_already_invoiced_outranks_everything_else():
    # An approved period must not be reclassified by a later change: once the
    # lines are on an invoice, they are on an invoice.
    for over in (
        {"switched_off": True},
        {"blocked_reason": "period_closed"},
        {"attribution_status": "Ambiguous"},
        {"attribution_status": "Unmapped"},
    ):
        state, reason = C.classify_chargeability(
            evidence(invoice_source="SINV-0001", **over)
        )
        assert state == "already_invoiced", over
        assert reason == "invoice_source_tracked", over


def test_ambiguity_outranks_a_switched_off_arrangement():
    # We cannot say whose money it is, so nothing else can be said about it.
    state, _ = C.classify_chargeability(
        evidence(attribution_status="Ambiguous", switched_off=True)
    )

    assert state == "ambiguous"


def test_a_switched_off_arrangement_outranks_a_blocked_reason():
    # A decision is not waiting for anything, so it is not "blocked".
    state, _ = C.classify_chargeability(
        evidence(switched_off=True, blocked_reason="exchange_rate_missing")
    )

    assert state == "assigned_not_billable"


def test_an_unmapped_page_is_unassigned_even_when_something_else_is_wrong():
    # "Blocked from invoicing" describes a line that COULD be invoiced but
    # cannot be yet. A line with no payer is not that.
    state, reason = C.classify_chargeability(
        evidence(attribution_status="Unmapped", blocked_reason="exchange_rate_missing")
    )

    assert state == "unassigned"
    assert reason == "no_page_mapping"


def test_a_known_blocked_reason_is_reported_by_name():
    for reason in C.CHARGEABILITY_BLOCKED_REASONS:
        state, reported = C.classify_chargeability(evidence(blocked_reason=reason))
        assert state == "blocked", reason
        assert reported == reason


def test_an_unknown_blocked_reason_is_still_blocked_but_says_so_generically():
    state, reason = C.classify_chargeability(
        evidence(blocked_reason="a_reason_invented_next_quarter")
    )

    assert state == "blocked"
    assert reason == "blocked_reason"


def test_an_unrecognised_attribution_status_is_not_reported_as_a_missing_mapping():
    # "No page mapping" sends the reader to SET UP A MAPPING. "Unrecognised
    # status" sends them to look at the sync. They are different problems.
    state, reason = C.classify_chargeability(evidence(attribution_status="SomethingNew"))

    assert state == "unassigned"
    assert reason == "unrecognised_attribution_status"


def test_a_missing_evidence_record_is_unassigned_rather_than_an_exception():
    state, reason = C.classify_chargeability()

    assert state == "unassigned"
    assert reason == "unrecognised_attribution_status"


def test_the_rule_never_returns_a_state_outside_the_vocabulary():
    cases = [
        {},
        evidence(),
        evidence(attribution_status="Unmapped"),
        evidence(attribution_status="Ambiguous"),
        evidence(switched_off=True),
        evidence(invoice_source="SINV-0001"),
        evidence(blocked_reason="period_closed"),
        evidence(attribution_status="Unmapped", switched_off=True, invoice_source="X"),
    ]

    for case in cases:
        state, reason = C.classify_chargeability(case)
        assert state in C.CHARGEABILITY_STATES, case
        assert reason in C.CHARGEABILITY_REASONS[state], case


def test_the_rule_table_is_in_the_order_the_docstring_states():
    # The order IS the precedence. A table reordered is a different rule.
    assert [rule[0] for rule in C.CHARGEABILITY_RULES] == [
        "has_invoice_source",
        "is_ambiguous",
        "is_switched_off",
        "is_unmapped",
        "has_blocked_reason",
        "is_attributed",
    ]


def test_it_is_pure():
    # The module must not need a site, so the rule can be exercised by the
    # parity guard and by a reviewer standing at a terminal.
    assert not hasattr(C, "frappe")


@pytest.mark.parametrize(
    "state,expected_billable",
    [
        ("assigned_billable", True),
        ("assigned_not_billable", False),
        ("unassigned", False),
        ("ambiguous", False),
        ("already_invoiced", False),
        ("blocked", False),
    ],
)
def test_only_the_billable_state_is_billable(state, expected_billable):
    assert C.is_billable_state(state) is expected_billable
