"""Tests for the balance rollup.

─── WHY THESE RUN WITHOUT FRAPPE ───────────────────────────────────────────────

The rollup is pure arithmetic over child rows, so it is tested here, with no
site, no database and no `import frappe`. That is the whole reason the
computation was split out of the controller.

This is the FIRST test suite the LaraTik Cashbox Frappe app has ever had. The
app had nine Python files and zero `test_*.py`, and the one behaviour it
implements — keeping the intended scope owner across Frappe's insert
lifecycle — had never been executed anywhere.

─── WHAT CHANGED WITH ADR-169 ─────────────────────────────────────────────────

The rollup used to classify rows by their `transaction_type`, against three
frozensets that the application mirrored in TypeScript. It now reads the
ENTRY's own fields — `direction`, `status`, `variance_type` — so there is
nothing left to keep in step with the other side of a REST call.

The tests below are written against the NEW model: an unclassifiable row is
in neither total, actual variance movements follow their direction, a rejected
row never happened, and a missing amount is not a zero.

Run them with:

    cd laratik_cashbox && python3 -m pytest tests/ -q
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest

_APP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "laratik_cashbox",
)

# The rollup imports the currency policy, which lives at the module's root,
# and the policy is pure — so it can be imported here with no site.
sys.path.insert(0, _APP)
sys.path.insert(
    0,
    os.path.join(_APP, "laratik_cashbox", "doctype", "laratik_cash_box"),
)

from rollup import (  # noqa: E402
    DEFAULT_BALANCE_RULE,
    RULE_OUTSTANDING,
    RULE_RECEIVED_MINUS_DISTRIBUTED,
    compute_rollup as _compute_rollup,
)


def compute_rollup(entries, pending_items=(), rule=DEFAULT_BALANCE_RULE, box_currency_value="USD"):
    """Use USD as the default test fixture denomination; production passes it explicitly."""
    return _compute_rollup(entries, pending_items, rule, box_currency_value)


def entry(**kwargs):
    """An entry row, with the fields a rollup reads."""
    row = {
        "entry_date": "2026-10-01",
        "direction": "In",
        "amount": 100.0,
        "currency": "USD",
        "category": "Cash",
        "status": "Completed",
        "approval_status": "Not Required",
        "variance_type": "None",
    }
    row.update(kwargs)
    return row


def item(**kwargs):
    """A pending roster row — one outstanding obligation."""
    row = {
        "party": "Acme",
        "amount": 50.0,
        "currency": "USD",
        "status": "Open",
    }
    row.update(kwargs)
    return row


# ─── THE AXES ─────────────────────────────────────────────────────────────────


class TestDirection:
    def test_an_incoming_entry_is_received(self):
        result = compute_rollup([entry(direction="In", amount=100.0)])
        assert result["total_received"] == 100.0
        assert result["total_distributed"] == 0.0

    def test_an_outgoing_entry_is_distributed(self):
        result = compute_rollup([entry(direction="Out", amount=100.0)])
        assert result["total_distributed"] == 100.0
        assert result["total_received"] == 0.0

    def test_an_entry_with_no_direction_is_in_neither_total(self):
        # An unclassifiable row must not be counted as incoming by default.
        # The direction says which way the money went; guessing wrong is how
        # a balance gets bigger than it is.
        result = compute_rollup([entry(direction="", amount=100.0)])
        assert result["total_received"] == 0.0
        assert result["total_distributed"] == 0.0

    def test_an_entry_with_an_unknown_direction_is_in_neither_total(self):
        result = compute_rollup([entry(direction="Sideways", amount=100.0)])
        assert result["total_received"] == 0.0
        assert result["total_distributed"] == 0.0


class TestStatus:
    def test_a_pending_entry_is_an_obligation_not_a_movement(self):
        # THIS IS THE CHANGE. The old rollup never read `status`, so a
        # `Cash Received` the application's own screens showed as PENDING
        # counted towards total_received. The rollup and the screens
        # disagreed, and the rollup produced the number.
        result = compute_rollup([entry(direction="In", status="Pending", amount=100.0)])
        assert result["total_received"] == 0.0
        assert result["total_distributed"] == 0.0
        assert result["total_outstanding"] == 0.0

    def test_a_completed_entry_is_a_movement(self):
        result = compute_rollup([entry(direction="In", status="Completed", amount=100.0)])
        assert result["total_received"] == 100.0

    def test_a_settled_entry_is_a_movement(self):
        result = compute_rollup([entry(direction="In", status="Settled", amount=100.0)])
        assert result["total_received"] == 100.0

    def test_a_cancelled_entry_happened_nowhere(self):
        result = compute_rollup([entry(direction="In", status="Cancelled", amount=100.0)])
        assert result["total_received"] == 0.0
        assert result["total_distributed"] == 0.0

    def test_a_preapproval_row_is_not_a_completed_movement(self):
        result = compute_rollup(
            [entry(direction="In", status="Completed", approval_status="Pending", amount=100.0)]
        )
        assert result["total_received"] == 0.0
        assert result["total_cash"] == 0.0

    def test_a_draft_document_is_not_a_completed_movement(self):
        result = compute_rollup([entry(direction="In", standalone_docstatus=0, amount=100.0)])
        assert result["total_received"] == 0.0

    def test_a_submitted_document_counts_once(self):
        result = compute_rollup([entry(direction="In", standalone_docstatus=1, amount=100.0)])
        assert result["total_received"] == 100.0


class TestVariance:
    def test_a_variance_counts_the_actual_movement_once(self):
        result = compute_rollup(
            [entry(direction="In", variance_type="Overpayment", amount=100.0)]
        )
        assert result["total_received"] == 100.0
        assert result["total_distributed"] == 0.0

    def test_an_underpayment_counts_the_actual_movement_once(self):
        result = compute_rollup(
            [entry(direction="In", variance_type="Underpayment", amount=100.0)]
        )
        assert result["total_received"] == 100.0
        assert result["total_distributed"] == 0.0

    def test_a_matching_variance_is_just_a_movement(self):
        result = compute_rollup(
            [entry(direction="In", variance_type="Match", amount=100.0)]
        )
        assert result["total_received"] == 100.0


# ─── THE PENDING ROSTER ───────────────────────────────────────────────────────


class TestPendingRoster:
    def test_the_roster_is_the_obligation_measure(self):
        result = compute_rollup([], [item(amount=50.0), item(amount=25.0)])
        assert result["total_outstanding"] == 75.0

    def test_a_settled_row_leaves_the_measure(self):
        result = compute_rollup(
            [], [item(amount=50.0), item(amount=25.0, status="Settled")]
        )
        assert result["total_outstanding"] == 50.0

    def test_a_party_is_a_row_not_a_column(self):
        # The old model had one column per named person, so the rollup matched
        # a free-text counterparty against three hardcoded names. Now it sums
        # the rows, and the application groups them.
        result = compute_rollup(
            [],
            [item(party="mohamad", amount=10.0), item(party="acme", amount=20.0)],
        )
        assert result["total_outstanding"] == 30.0
        assert not hasattr(result, "pending_mohamad")

    def test_the_roster_counts_its_open_rows(self):
        result = compute_rollup(
            [], [item(amount=10.0), item(amount=20.0, status="Settled")]
        )
        assert result["pending_count"] == 1

    def test_an_unreadable_roster_row_is_not_a_zero(self):
        result = compute_rollup([], [item(amount=50.0), item(amount="")])
        assert result["total_outstanding"] == 50.0
        assert result["readable"] is False


# ─── THE BALANCE RULE ─────────────────────────────────────────────────────────


class TestBalanceRule:
    def test_the_main_box_is_received_minus_distributed(self):
        result = compute_rollup(
            [
                entry(direction="In", amount=100.0),
                entry(direction="Out", amount=30.0),
            ],
            rule=RULE_RECEIVED_MINUS_DISTRIBUTED,
        )
        assert result["current_balance"] == 70.0

    def test_an_ordinary_box_is_its_outstanding(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0)],
            [item(amount=40.0)],
            rule=RULE_OUTSTANDING,
        )
        assert result["current_balance"] == 40.0

    def test_the_default_is_the_rule_that_cannot_change_an_existing_figure(self):
        # `Outstanding` is what every box except the main one uses, so it is
        # the only default that cannot change a figure somebody already has.
        assert DEFAULT_BALANCE_RULE == RULE_OUTSTANDING

    def test_an_unknown_rule_falls_back_to_the_conservative_one(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0)],
            [item(amount=40.0)],
            rule="Something else entirely",
        )
        assert result["current_balance"] == 40.0


# ─── THE OPERATIONAL CASH ─────────────────────────────────────────────────────


class TestTotalCash:
    """`total_cash` is the cash that MOVED, for every box, whatever the rule.

    It needs its own name because `current_balance` is the rule-selected
    figure and ADR-167 schedules that for retirement when the general ledger
    becomes the balance. Without a name of its own, the rollup's cash measure
    would lose its home — and the operational cash the till actually moved and
    the accounting cash the GL says the account holds would compete for one
    field name. They are different facts (ADR-170).
    """

    def test_it_is_computed_for_every_box_whatever_the_rule(self):
        entries = [entry(direction="In", amount=100.0), entry(direction="Out", amount=30.0)]
        for rule in (RULE_RECEIVED_MINUS_DISTRIBUTED, RULE_OUTSTANDING):
            result = compute_rollup(entries, [], rule=rule)
            assert result["total_cash"] == 70.0

    def test_it_equals_current_balance_under_the_cash_rule(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0)], rule=RULE_RECEIVED_MINUS_DISTRIBUTED
        )
        assert result["total_cash"] == result["current_balance"] == 100.0

    def test_it_differs_from_current_balance_under_the_obligation_rule(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0)],
            [item(amount=40.0)],
            rule=RULE_OUTSTANDING,
        )
        assert result["total_cash"] == 100.0
        assert result["current_balance"] == 40.0

    def test_a_pending_entry_is_not_in_it(self):
        result = compute_rollup([entry(direction="In", status="Pending", amount=100.0)])
        assert result["total_cash"] == 0.0

    def test_a_transfer_moves_it_without_being_a_receipt(self):
        result = compute_rollup([entry(direction="In", amount=100.0, other_box="box-2")])
        assert result["total_cash"] == 100.0
        assert result["total_received"] == 0.0


# ─── THE CURRENCY RULE ───────────────────────────────────────────────────────


class TestCurrency:
    """A row in another currency is REFUSED, not converted.

    The rule lives in `currency_policy` (pure, no Frappe) and the CALL SITE is
    here, in the row loop, because a refused row must contribute nothing AND
    be counted as unreadable — so the controller warns and the application
    renders "unreadable" rather than a total that quietly adds two currencies.
    """

    def test_a_row_in_another_currency_is_refused(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0, currency="EUR")],
            [],
            box_currency_value="USD",
        )
        assert result["total_received"] == 0.0
        assert result["readable"] is False
        assert result["unreadable_count"] == 1

    def test_a_row_in_the_box_currency_is_summed(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0, currency="USD")],
            [],
            box_currency_value="USD",
        )
        assert result["total_received"] == 100.0
        assert result["readable"] is True

    def test_a_row_that_names_no_currency_is_unverified_and_excluded(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0, currency="")],
            [],
            box_currency_value="USD",
        )
        assert result["total_received"] == 0.0
        assert result["readable"] is False
        assert result["unreadable_count"] == 1

    def test_an_unknown_box_currency_makes_the_total_unverified(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0, currency="EUR")],
            [],
            box_currency_value=None,
        )
        assert result["total_received"] == 0.0
        assert result["readable"] is False
        assert result["unreadable_count"] == 1

    def test_a_roster_row_in_another_currency_is_refused(self):
        result = compute_rollup(
            [],
            [item(amount=50.0, currency="EUR")],
            box_currency_value="USD",
            rule=RULE_OUTSTANDING,
        )
        assert result["total_outstanding"] == 0.0
        assert result["current_balance"] == 0.0
        assert result["readable"] is False

    def test_a_roster_row_in_the_box_currency_is_summed(self):
        result = compute_rollup(
            [],
            [item(amount=50.0, currency="USD")],
            box_currency_value="USD",
            rule=RULE_OUTSTANDING,
        )
        assert result["total_outstanding"] == 50.0


# ─── LOANS ────────────────────────────────────────────────────────────────────


class TestLoans:
    def test_a_loan_given_is_outgoing_money(self):
        result = compute_rollup([entry(direction="Out", category="Loan", amount=500.0)])
        assert result["total_distributed"] == 500.0

    def test_a_loan_repaid_is_incoming_money(self):
        result = compute_rollup([entry(direction="In", category="Loan", amount=500.0)])
        assert result["total_received"] == 500.0

    def test_the_obligation_is_the_roster_not_a_formula(self):
        # The old model computed the loan balance from two movement types:
        # issued + loan given - loan repaid. Now the obligation is a ROW, and
        # it is settled by the row's status.
        result = compute_rollup(
            [entry(direction="Out", category="Loan", amount=500.0)],
            [item(amount=500.0)],
            rule=RULE_OUTSTANDING,
        )
        assert result["total_outstanding"] == 500.0


# ─── TRANSFERS ────────────────────────────────────────────────────────────────


class TestTransfers:
    def test_a_transfer_in_moves_the_balance_but_is_not_a_receipt(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0, other_box="box-2")],
            rule=RULE_RECEIVED_MINUS_DISTRIBUTED,
        )
        assert result["total_received"] == 0.0
        assert result["current_balance"] == 100.0

    def test_a_transfer_out_reduces_the_balance(self):
        result = compute_rollup(
            [entry(direction="Out", amount=100.0, other_box="box-2")],
            rule=RULE_RECEIVED_MINUS_DISTRIBUTED,
        )
        assert result["total_distributed"] == 0.0
        assert result["current_balance"] == -100.0

    def test_a_cancelled_transfer_does_not_count(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0, other_box="box-2", status="Cancelled")],
            rule=RULE_RECEIVED_MINUS_DISTRIBUTED,
        )
        assert result["current_balance"] == 0.0

    def test_a_transfer_is_not_an_obligation(self):
        result = compute_rollup(
            [entry(direction="Out", amount=100.0, other_box="box-2")],
            [],
            rule=RULE_OUTSTANDING,
        )
        assert result["total_outstanding"] == 0.0


# ─── EXCLUSIONS ───────────────────────────────────────────────────────────────


class TestExclusions:
    def test_a_rejected_entry_never_happened(self):
        result = compute_rollup(
            [entry(direction="In", amount=100.0, approval_status="Rejected")]
        )
        assert result["total_received"] == 0.0

    def test_a_rejected_transfer_never_happened(self):
        result = compute_rollup(
            [
                entry(
                    direction="In",
                    amount=100.0,
                    other_box="box-2",
                    approval_status="Rejected",
                )
            ],
            rule=RULE_RECEIVED_MINUS_DISTRIBUTED,
        )
        assert result["current_balance"] == 0.0


# ─── UNREADABLE AMOUNTS ───────────────────────────────────────────────────────


class TestUnreadable:
    @pytest.mark.parametrize("amount", [None, "", "not a number", float("inf"), float("nan")])
    def test_an_unreadable_amount_is_not_a_zero(self, amount):
        result = compute_rollup([entry(direction="In", amount=amount)])
        assert result["total_received"] == 0.0
        assert result["readable"] is False

    def test_a_readable_rollup_says_so(self):
        result = compute_rollup([entry(direction="In", amount=100.0)])
        assert result["readable"] is True
        assert result["unreadable_count"] == 0


class TestDecimalArithmetic:
    def test_fractional_values_sum_without_binary_float_drift(self):
        result = compute_rollup(
            [entry(direction="In", amount="0.10"), entry(direction="In", amount="0.20")]
        )
        assert result["total_received"] == Decimal("0.30")
        assert result["total_cash"] == Decimal("0.30")

    def test_decimal_input_is_preserved(self):
        result = compute_rollup([entry(direction="Out", amount=Decimal("1.005"))])
        assert result["total_distributed"] == Decimal("1.005")


# ─── COUNTS AND DATES ─────────────────────────────────────────────────────────


class TestCountsAndDates:
    def test_last_activity_is_the_latest_date(self):
        result = compute_rollup(
            [
                entry(direction="In", entry_date="2026-10-01"),
                entry(direction="In", entry_date="2026-10-09"),
                entry(direction="In", entry_date="2026-10-05"),
            ]
        )
        assert result["last_activity"] == "2026-10-09"

    def test_a_box_with_nothing_in_it_has_no_last_activity(self):
        assert compute_rollup([])["last_activity"] is None

    def test_a_pending_entry_does_not_change_last_movement_date(self):
        result = compute_rollup(
            [entry(direction="In", status="Pending", entry_date="2026-10-07")]
        )
        assert result["last_activity"] is None


# ─── THE WHOLE FIGURE ─────────────────────────────────────────────────────────


class TestTheWholeFigure:
    def test_a_realistic_box(self):
        entries = [
            entry(direction="In", category="Service Payment", amount=1000.0),
            entry(direction="In", category="Vendor Payment", amount=200.0),
            entry(direction="Out", category="Salary", amount=300.0),
            entry(direction="Out", category="Loan", amount=500.0),
            entry(direction="Out", category="Invoice Issued", status="Pending", amount=80.0),
            entry(direction="In", category="Invoice Issued", status="Completed", amount=800.0),
            entry(direction="In", amount=250.0, other_box="box-2"),
        ]
        roster = [item(party="mohamad", amount=500.0), item(party="acme", amount=80.0)]

        cash = compute_rollup(entries, roster, rule=RULE_RECEIVED_MINUS_DISTRIBUTED)
        assert cash["total_received"] == 1200.0
        assert cash["total_distributed"] == 800.0
        assert cash["current_balance"] == 1200.0 + 250.0 - 800.0

        custody = compute_rollup(entries, roster, rule=RULE_OUTSTANDING)
        assert custody["total_outstanding"] == 580.0
        assert custody["current_balance"] == 580.0

    def test_an_empty_box_is_zero_and_says_it_is_readable(self):
        result = compute_rollup([], [])
        assert result["current_balance"] == 0.0
        assert result["readable"] is True
