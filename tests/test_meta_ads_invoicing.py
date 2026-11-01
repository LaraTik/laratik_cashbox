"""What may be INVOICED, and what must be decided before it is.

The module under test is PURE, so these tests need no site and no Frappe
fake. They hold the three things that matter:

1. A fee that nobody stated is **unconfigured**, and it is NOT zero.
2. A **recharge** invoice and a **service-fee-only** invoice are different
   numbers, and the module never chooses between them.
3. A period that is missing a DECISION is `needs_approval`; a period missing
   its DATA is `blocked`; and only `ready` may be invoiced.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from laratik_cashbox import meta_ads_invoicing as invoicing


# ─── THE FEE ───────────────────────────────────────────────────────────────────

def test_a_profile_with_no_stated_fee_is_unconfigured_and_not_zero():
    # A profile nobody ever configured reads as a zero fee, and a zero fee is
    # a legitimate answer. The two must not be the same thing.
    status, percent, source = invoicing.resolve_fee([], "0", False, date(2026, 10, 2))

    assert status == invoicing.FEE_STATUS_UNCONFIGURED
    assert percent == Decimal(0)
    assert source == ""


def test_a_stated_fee_is_configured():
    status, percent, source = invoicing.resolve_fee([], "15", True, date(2026, 10, 2))

    assert status == invoicing.FEE_STATUS_CONFIGURED
    assert percent == Decimal("15")
    assert source == "profile"


def test_an_approved_zero_is_a_configured_zero():
    # Somebody wrote the zero down. That is a decision.
    status, percent, source = invoicing.resolve_fee([], "0", True, date(2026, 10, 2))

    assert status == invoicing.FEE_STATUS_CONFIGURED
    assert percent == Decimal(0)


def test_a_stated_percentage_is_configured_even_without_the_flag():
    # THE FAILURE THIS FIXES.
    #
    # `fee_is_configured` exists to tell an APPROVED ZERO apart from NO FEE
    # AT ALL. It is not the only way a fee gets stated: a profile whose
    # standing fee is 15% had its fee stated by somebody typing 15, whether
    # or not they also ticked a flag that was added to the schema later.
    #
    # Reading the flag as the ONLY authority would silently charge 0% on
    # every profile that predates the flag — the worst possible way to be
    # wrong about money — and it would do it silently, because the fee would
    # still be "configured" and the invoice would still be creatable.
    status, percent, source = invoicing.resolve_fee([], "15", False, date(2026, 10, 2))

    assert status == invoicing.FEE_STATUS_CONFIGURED
    assert percent == Decimal("15")
    assert source == "profile"


def test_a_dated_rule_is_configured_even_when_it_is_zero():
    rules = [{"effective_from": date(2026, 10, 1), "fee_percent": Decimal(0)}]
    status, percent, source = invoicing.resolve_fee(rules, "0", False, date(2026, 10, 2))

    assert status == invoicing.FEE_STATUS_CONFIGURED
    assert percent == Decimal(0)
    assert source == "dated rule"


def test_the_latest_applicable_dated_rule_wins():
    rules = [
        {"effective_from": date(2026, 10, 1), "fee_percent": Decimal(15)},
        {"effective_from": date(2026, 10, 15), "fee_percent": Decimal(20)},
    ]

    assert invoicing.resolve_fee(rules, "0", False, date(2026, 10, 10))[1] == Decimal(15)
    assert invoicing.resolve_fee(rules, "0", False, date(2026, 10, 16))[1] == Decimal(20)


def test_a_rule_that_is_not_yet_effective_does_not_apply():
    rules = [{"effective_from": date(2026, 11, 1), "fee_percent": Decimal(20)}]

    # Before the rule starts, the standing fee applies — and only if it was
    # stated.
    assert invoicing.resolve_fee(rules, "0", False, date(2026, 10, 31))[0] == invoicing.FEE_STATUS_UNCONFIGURED
    assert invoicing.resolve_fee(rules, "15", True, date(2026, 10, 31))[1] == Decimal(15)


def test_an_iso_date_in_a_rule_is_read():
    rules = [{"effective_from": "2026-10-01", "fee_percent": "15"}]

    status, percent, _source = invoicing.resolve_fee(rules, "0", False, date(2026, 10, 2))

    assert status == invoicing.FEE_STATUS_CONFIGURED
    assert percent == Decimal(15)


# ─── THE CHARGE ────────────────────────────────────────────────────────────────

def test_a_recharge_invoice_carries_the_spend_and_the_fee():
    charge = invoicing.charge_amount(invoicing.CHARGE_MODEL_RECHARGE, Decimal("100.00"), Decimal("15.00"))

    assert charge == Decimal("115.00")


def test_a_service_fee_only_invoice_carries_the_fee_alone():
    charge = invoicing.charge_amount(
        invoicing.CHARGE_MODEL_SERVICE_FEE_ONLY, Decimal("100.00"), Decimal("15.00")
    )

    assert charge == Decimal("15.00")


def test_an_undecided_charge_model_refuses_to_compute_a_charge():
    # The module never picks one. A profile that has not answered P-2 has no
    # number, and that is the honest answer.
    with pytest.raises(ValueError, match="charge model is not decided"):
        invoicing.charge_amount("", Decimal("100.00"), Decimal("15.00"))


def test_the_fee_is_the_basis_at_the_stated_percentage():
    assert invoicing.fee_amount(Decimal("100.00"), Decimal("15")) == Decimal("15.00")
    assert invoicing.fee_amount(Decimal("33.33"), Decimal("15")) == Decimal("4.9995")


# ─── THE READINESS ─────────────────────────────────────────────────────────────

def test_a_fully_stated_period_is_ready():
    state, reasons = invoicing.classify_readiness({
        "chargeable_spend": Decimal("100.00"),
        "blocked_reasons": [],
        "approval_reasons": [],
    })

    assert state == invoicing.READINESS_READY
    assert reasons == []


def test_a_missing_decision_is_needs_approval_and_not_blocked():
    # A human settles this. Nothing is wrong with the data.
    state, reasons = invoicing.classify_readiness({
        "chargeable_spend": Decimal("100.00"),
        "blocked_reasons": [],
        "approval_reasons": ["fee_unconfigured"],
    })

    assert state == invoicing.READINESS_NEEDS_APPROVAL
    assert reasons == ["fee_unconfigured"]


def test_missing_data_is_blocked_and_not_an_approval():
    # No approval fixes a missing exchange rate.
    state, reasons = invoicing.classify_readiness({
        "chargeable_spend": Decimal("100.00"),
        "blocked_reasons": ["exchange_rate_missing"],
        "approval_reasons": [],
    })

    assert state == invoicing.READINESS_BLOCKED
    assert reasons == ["exchange_rate_missing"]


def test_blocked_wins_over_needs_approval():
    # The data problem is the bigger one: approving the terms would still
    # leave the invoice uncomputable.
    state, reasons = invoicing.classify_readiness({
        "chargeable_spend": Decimal("100.00"),
        "blocked_reasons": ["exchange_rate_missing"],
        "approval_reasons": ["fee_unconfigured"],
    })

    assert state == invoicing.READINESS_BLOCKED
    assert reasons == ["exchange_rate_missing"]


def test_an_already_invoiced_period_is_never_invoiced_again():
    # Whatever else has changed since, this period is done.
    state, reasons = invoicing.classify_readiness({
        "chargeable_spend": Decimal("100.00"),
        "already_invoiced": True,
        "blocked_reasons": [],
        "approval_reasons": [],
    })

    assert state == invoicing.READINESS_ALREADY_INVOICED
    assert reasons == ["already_invoiced"]


def test_an_already_invoiced_period_wins_over_a_missing_decision():
    state, _reasons = invoicing.classify_readiness({
        "chargeable_spend": Decimal("100.00"),
        "already_invoiced": True,
        "approval_reasons": ["fee_unconfigured"],
    })

    assert state == invoicing.READINESS_ALREADY_INVOICED


def test_a_period_with_no_spend_has_nothing_to_invoice():
    # An empty period must not report a missing fee. There is nothing to
    # decide, because there is nothing to charge.
    state, reasons = invoicing.classify_readiness({
        "chargeable_spend": Decimal("0"),
        "blocked_reasons": [],
        "approval_reasons": ["fee_unconfigured"],
    })

    assert state == invoicing.READINESS_NOTHING_TO_INVOICE
    assert reasons == ["no_chargeable_spend"]


def test_nothing_to_invoice_wins_over_blocked():
    state, _reasons = invoicing.classify_readiness({
        "chargeable_spend": Decimal("0"),
        "blocked_reasons": ["exchange_rate_missing"],
    })

    assert state == invoicing.READINESS_NOTHING_TO_INVOICE


# ─── THE VOCABULARIES ──────────────────────────────────────────────────────────

def test_the_readiness_states_are_five():
    assert invoicing.READINESS_STATES == (
        "ready",
        "needs_approval",
        "blocked",
        "already_invoiced",
        "nothing_to_invoice",
    )


def test_the_readiness_reasons_do_not_overlap_between_the_two_kinds():
    # A reason names ONE thing. A reason that could mean "missing" for one
    # kind of gap and "undecided" for another would need two labels to stay
    # unambiguous.
    assert not set(invoicing.NEEDS_APPROVAL_REASONS) & set(invoicing.BLOCKED_REASONS)


def test_the_readiness_rule_table_names_every_state():
    assert {rule["state"] for rule in invoicing.READINESS_RULES} == set(invoicing.READINESS_STATES)


def test_the_already_invoiced_rule_comes_first():
    # An invoiced period is never invoiced again, whatever else changed.
    assert invoicing.READINESS_RULES[0]["id"] == "already_invoiced"


def test_the_charge_models_are_the_two_the_brief_names():
    assert invoicing.CHARGE_MODELS == ("Recharge", "Service fee only")


def test_the_recognition_models_are_the_two_the_brief_names():
    assert invoicing.RECOGNITION_MODELS == ("Principal", "Agent")
