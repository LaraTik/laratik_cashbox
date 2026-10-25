"""The reversal rule (ADR-177).

─── WHY THIS FILE EXISTS ──────────────────────────────────────────────────────

A cancellation and a reversal are DIFFERENT facts. A cancellation says *this
never happened*; a reversal says *this happened, and here is the opposite
leg*.

The application could do the second only by doing the first: a movement whose
money had already moved could be switched to ``Cancelled``, and the rollup
drops the row — so the box's cash rose by the row's amount, with no leg
anywhere and nothing in the ledger saying why.

These tests are the receipt for the rule that decides. They run without
ERPNext: the rule is a pure function of the rows, and the same reason
``rollup.py`` is pure is the reason this one is.

The LAST class in this file is the one that matters most — it runs the real
rollup over a reversed pair and shows the pair nets to zero, and then shows
what the old silent cancellation did to the same box.

Run them with:

    cd laratik_cashbox && python3 -m pytest tests/ -q
"""

from __future__ import annotations

import os
import sys

import pytest

from laratik_cashbox.reversal import (
    ALLOWED,
    COUNTED_STATUSES,
    ENTRY_STATUSES,
    REFUSED_ALREADY_REVERSED,
    REFUSED_MONEY_ALREADY_MOVED,
    REFUSED_NOTHING_TO_REVERSE,
    REFUSED_NO_NAME,
    REFUSED_REVERSED_IS_SET_BY_THE_REVERSAL,
    REFUSED_TERMINAL,
    REFUSED_UNKNOWN_DIRECTION,
    REFUSED_UNKNOWN_STATUS,
    STATUS_REVERSED,
    TERMINAL_STATUSES,
    UNCOUNTED_STATUSES,
    already_reversed,
    build_reversal_leg,
    duplicate_reversal_targets,
    orphan_reversal_legs,
    reversal_is_allowed,
    status_change_is_allowed,
    status_counts,
    unjustified_reversed_rows,
)

_APP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "laratik_cashbox",
)

# The rollup imports the currency policy, which lives at the module's root,
# and the policy is pure — so it can be imported here with no site.
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, "laratik_cashbox", "doctype", "laratik_cash_box"))

from rollup import RULE_RECEIVED_MINUS_DISTRIBUTED, compute_rollup  # noqa: E402


def row(name: str = "", **overrides):
    """An entry row, with the fields this rule reads."""
    base = {
        "name": name,
        "status": "Completed",
        "direction": "In",
        "amount": 100,
        "currency": "USD",
        "category": "Cash",
        "variance_type": "None",
        "other_box": "",
        "counterparty": "",
        "funding_source": "Company Cash",
        "money_flow": "From Client to Us",
        "reversal_of": "",
        "idempotency_key": "",
    }
    base.update(overrides)
    return base


class TestTheVocabulary:
    def test_the_vocabulary_is_five_values(self):
        assert ENTRY_STATUSES == (
            "Pending",
            "Completed",
            "Settled",
            "Cancelled",
            STATUS_REVERSED,
        )

    def test_reversed_is_not_cancelled(self):
        # The whole design rests on this: a reversed row DID count, and a
        # cancelled row did not. If `Reversed` were `Cancelled`, the pair
        # would not net to zero.
        assert STATUS_REVERSED not in UNCOUNTED_STATUSES
        assert STATUS_REVERSED in COUNTED_STATUSES
        assert STATUS_REVERSED in TERMINAL_STATUSES

    def test_a_row_counts_only_when_its_money_moved(self):
        assert status_counts(row(status="Completed")) is True
        assert status_counts(row(status="Settled")) is True
        assert status_counts(row(status=STATUS_REVERSED)) is True
        assert status_counts(row(status="Pending")) is False
        assert status_counts(row(status="Cancelled")) is False


class TestStatusChanges:
    def test_a_pending_movement_may_be_cancelled(self):
        # It never moved, so removing it changes no figure. This is the
        # operation that is still allowed.
        assert status_change_is_allowed(row(status="Pending"), "Cancelled") == (True, ALLOWED)

    def test_a_movement_whose_money_moved_may_not_be_cancelled(self):
        # THE DEFECT. Cancelling this row drops its amount out of the cash
        # measure with no leg anywhere.
        assert status_change_is_allowed(row(status="Completed"), "Cancelled") == (
            False,
            REFUSED_MONEY_ALREADY_MOVED,
        )
        assert status_change_is_allowed(row(status="Settled"), "Cancelled") == (
            False,
            REFUSED_MONEY_ALREADY_MOVED,
        )

    def test_a_movement_whose_money_moved_may_not_go_back_to_pending(self):
        # THE SAME DEFECT, THE OTHER DOOR.
        #
        # `Pending` is outside the counted set exactly as `Cancelled` is, so
        # moving a counted row back to it removes its amount from the cash
        # measure just as silently — and it was REACHABLE: the status picker
        # offered `Pending` on every row, and the approval workflow lives on
        # the `approval_status` axis, not this one, so nothing else needed
        # `status: "Pending"`.
        #
        # One rule closes both doors: a row whose money moved may not stop
        # counting.
        assert status_change_is_allowed(row(status="Completed"), "Pending") == (
            False,
            REFUSED_MONEY_ALREADY_MOVED,
        )
        assert status_change_is_allowed(row(status="Settled"), "Pending") == (
            False,
            REFUSED_MONEY_ALREADY_MOVED,
        )
        # A `Reversed` row is TERMINAL, and the terminal rule is applied
        # first — so it is refused as terminal, not as "the money moved".
        assert status_change_is_allowed(row(status=STATUS_REVERSED), "Pending") == (
            False,
            REFUSED_TERMINAL,
        )

    def test_cancelled_and_reversed_are_terminal(self):
        assert status_change_is_allowed(row(status="Cancelled"), "Completed") == (
            False,
            REFUSED_TERMINAL,
        )
        assert status_change_is_allowed(row(status="Cancelled"), "Pending") == (
            False,
            REFUSED_TERMINAL,
        )
        assert status_change_is_allowed(row(status=STATUS_REVERSED), "Completed") == (
            False,
            REFUSED_TERMINAL,
        )

    def test_reversed_cannot_be_set_by_a_caller(self):
        # Only the reversal path writes `Reversed`, and it writes it
        # together with the leg. A caller that could set it alone would
        # remove a movement from the cash measure with no leg — the exact
        # defect this rule exists to close.
        assert status_change_is_allowed(row(status="Completed"), STATUS_REVERSED) == (
            False,
            REFUSED_REVERSED_IS_SET_BY_THE_REVERSAL,
        )
        assert status_change_is_allowed(row(status="Pending"), STATUS_REVERSED) == (
            False,
            REFUSED_REVERSED_IS_SET_BY_THE_REVERSAL,
        )

    def test_an_unknown_status_is_refused(self):
        # A typo must not move a balance.
        assert status_change_is_allowed(row(status="Complete"), "Completed") == (
            False,
            REFUSED_UNKNOWN_STATUS,
        )
        assert status_change_is_allowed(row(status="Completed"), "complete") == (
            False,
            REFUSED_UNKNOWN_STATUS,
        )

    def test_the_ordinary_transitions_are_allowed(self):
        assert status_change_is_allowed(row(status="Pending"), "Completed") == (True, ALLOWED)
        assert status_change_is_allowed(row(status="Pending"), "Settled") == (True, ALLOWED)
        assert status_change_is_allowed(row(status="Completed"), "Settled") == (True, ALLOWED)

    def test_a_rejected_row_never_counted_so_it_may_be_cancelled(self):
        # The rollup drops a rejected row before the amount is read, so
        # cancelling it changes no figure.
        rejected = row(status="Completed", approval_status="Rejected")
        assert status_change_is_allowed(rejected, "Cancelled") == (True, ALLOWED)


class TestWhatMayBeReversed:
    def test_a_movement_whose_money_moved_may_be_reversed(self):
        allowed, reason = reversal_is_allowed(row("ROW-1"))
        assert (allowed, reason) == (True, ALLOWED)

    def test_a_pending_movement_may_not_be_reversed(self):
        # It never moved. Cancelling it is the right operation; reversing it
        # would post a leg against money that never left.
        allowed, reason = reversal_is_allowed(row("ROW-1", status="Pending"))
        assert (allowed, reason) == (False, REFUSED_NOTHING_TO_REVERSE)

    def test_a_cancelled_movement_may_not_be_reversed(self):
        allowed, reason = reversal_is_allowed(row("ROW-1", status="Cancelled"))
        assert (allowed, reason) == (False, REFUSED_NOTHING_TO_REVERSE)

    def test_a_rejected_movement_may_not_be_reversed(self):
        # A rejected row never happened: the rollup drops it before the
        # amount is read. A leg against it would be COUNTED while the row it
        # reverses was not — so the pair would move the balance instead of
        # cancelling it.
        allowed, reason = reversal_is_allowed(
            row("ROW-1", approval_status="Rejected")
        )
        assert (allowed, reason) == (False, REFUSED_NOTHING_TO_REVERSE)

    def test_a_movement_that_already_has_its_leg_may_not_be_reversed_again(self):
        original = row("ROW-1")
        leg = row("ROW-2", reversal_of="ROW-1")
        allowed, reason = reversal_is_allowed(original, [original, leg])
        assert (allowed, reason) == (False, REFUSED_ALREADY_REVERSED)

    def test_a_row_with_no_name_may_not_be_reversed(self):
        # A leg that cannot name what it reverses is a leg nobody can trace.
        allowed, reason = reversal_is_allowed(row(""))
        assert (allowed, reason) == (False, REFUSED_NO_NAME)

    def test_a_row_with_an_unclassifiable_direction_may_not_be_reversed(self):
        allowed, reason = reversal_is_allowed(row("ROW-1", direction="Sideways"))
        assert (allowed, reason) == (False, REFUSED_UNKNOWN_DIRECTION)

    def test_already_reversed_reads_the_legs_own_field(self):
        original = row("ROW-1")
        assert already_reversed(original, [original]) is False
        assert already_reversed(original, [original, row("ROW-2", reversal_of="ROW-1")]) is True


class TestTheLeg:
    def test_the_leg_is_the_opposite_movement(self):
        leg = build_reversal_leg(
            row("ROW-1", direction="In", amount=250),
            entry_date="2026-10-23",
            idempotency_key="rev-key-0001",
        )
        assert leg["direction"] == "Out"
        assert leg["amount"] == 250
        assert leg["currency"] == "USD"
        assert leg["reversal_of"] == "ROW-1"
        assert leg["status"] == "Completed"
        assert leg["idempotency_key"] == "rev-key-0001"

    def test_the_leg_flips_an_out_movement_to_in(self):
        leg = build_reversal_leg(
            row("ROW-1", direction="Out"), entry_date="2026-10-23", idempotency_key="rev-key-0001"
        )
        assert leg["direction"] == "In"

    def test_the_leg_keeps_variance_metadata(self):
        leg = build_reversal_leg(
            row("ROW-1", variance_type="Overpayment"),
            entry_date="2026-10-23",
            idempotency_key="rev-key-0001",
        )
        assert leg["variance_type"] == "Overpayment"

    def test_the_leg_keeps_the_other_box_so_a_reversed_transfer_is_still_a_transfer(self):
        leg = build_reversal_leg(
            row("ROW-1", other_box="BOX-2"),
            entry_date="2026-10-23",
            idempotency_key="rev-key-0001",
        )
        assert leg["other_box"] == "BOX-2"

    def test_the_leg_keeps_the_category_and_the_counterparty(self):
        leg = build_reversal_leg(
            row("ROW-1", category="Salary", counterparty="Acme"),
            entry_date="2026-10-23",
            idempotency_key="rev-key-0001",
        )
        assert leg["category"] == "Salary"
        assert leg["counterparty"] == "Acme"

    def test_the_leg_does_not_carry_the_money_flow(self):
        # `money_flow` says between whom the money moved, and the leg moves
        # the other way. Carrying the original's value over would state a
        # direction the row does not have.
        leg = build_reversal_leg(
            row("ROW-1", money_flow="From Client to Us"),
            entry_date="2026-10-23",
            idempotency_key="rev-key-0001",
        )
        assert "money_flow" not in leg


class TestTheBoxIsConsistent:
    def test_two_legs_against_one_original_are_a_duplicate(self):
        rows = [
            row("ROW-1"),
            row("ROW-2", reversal_of="ROW-1"),
            row("ROW-3", reversal_of="ROW-1"),
        ]
        assert duplicate_reversal_targets(rows) == [("ROW-1", "ROW-2")]

    def test_one_leg_per_original_is_not_a_duplicate(self):
        rows = [row("ROW-1"), row("ROW-2", reversal_of="ROW-1"), row("ROW-3", reversal_of="ROW-4")]
        assert duplicate_reversal_targets(rows) == []

    def test_a_leg_naming_a_missing_original_is_an_orphan(self):
        rows = [row("ROW-2", reversal_of="ROW-NOT-HERE")]
        assert orphan_reversal_legs(rows) == ["ROW-2"]

    def test_a_leg_naming_a_present_original_is_not_an_orphan(self):
        rows = [row("ROW-1"), row("ROW-2", reversal_of="ROW-1")]
        assert orphan_reversal_legs(rows) == []


class TestTheMarkAndItsLeg:
    """The mark and the leg are ONE fact, and the server refuses them apart."""

    def test_a_reversed_row_with_no_leg_is_unjustified(self):
        rows = [row("ROW-1", status=STATUS_REVERSED)]
        assert unjustified_reversed_rows(rows) == ["ROW-1"]

    def test_a_reversed_row_with_its_leg_is_justified(self):
        rows = [
            row("ROW-1", status=STATUS_REVERSED),
            row("ROW-2", reversal_of="ROW-1"),
        ]
        assert unjustified_reversed_rows(rows) == []

    def test_a_row_that_is_not_reversed_is_never_unjustified(self):
        rows = [row("ROW-1", status="Completed"), row("ROW-2", reversal_of="ROW-1")]
        assert unjustified_reversed_rows(rows) == []

    def test_a_box_with_no_reversals_has_no_unjustified_rows(self):
        assert unjustified_reversed_rows([]) == []


class TestTheArithmetic:
    """The proof that the mechanism works, run against the REAL rollup."""

    def test_a_reversed_pair_nets_the_cash_measure_to_zero(self):
        original = row("ROW-1", direction="In", amount=100)
        leg = build_reversal_leg(
            original, entry_date="2026-10-23", idempotency_key="rev-key-0001"
        )
        leg_row = row("ROW-2", **leg)

        result = compute_rollup(
            [original, leg_row],
            rule=RULE_RECEIVED_MINUS_DISTRIBUTED,
            box_currency_value="USD",
        )
        assert result["total_cash"] == 0.0
        assert result["readable"] is True

    def test_the_original_alone_still_counts(self):
        # The reversed row is NOT cancelled: the money moved, so it counts,
        # and the LEG is what takes the count back out.
        original = row("ROW-1", direction="In", amount=100)
        result = compute_rollup(
            [original], rule=RULE_RECEIVED_MINUS_DISTRIBUTED, box_currency_value="USD"
        )
        assert result["total_cash"] == 100.0

    def test_the_silent_cancellation_was_the_defect(self):
        # This is what the refusal prevents. Cancelling the row drops its
        # amount out of the cash measure — the box's cash RISES by 100 with
        # no leg and no trace, which for a transfer means the money is
        # physically in the other box and in the GL while this box's ledger
        # says it never happened.
        original = row("ROW-1", direction="In", amount=100)
        cancelled = row("ROW-1", direction="In", amount=100, status="Cancelled")

        after_cancel = compute_rollup(
            [cancelled], rule=RULE_RECEIVED_MINUS_DISTRIBUTED, box_currency_value="USD"
        )
        assert after_cancel["total_cash"] == 0.0
        assert after_cancel["total_cash"] != 100.0

    def test_a_reversed_transfer_is_still_a_transfer(self):
        # The leg keeps `other_box`, so it lands in the transfer totals and
        # not in received/distributed — the pair nets to zero there too.
        original = row("ROW-1", direction="Out", amount=100, other_box="BOX-2")
        leg = build_reversal_leg(
            original, entry_date="2026-10-23", idempotency_key="rev-key-0001"
        )
        leg_row = row("ROW-2", **leg)

        result = compute_rollup(
            [original, leg_row],
            rule=RULE_RECEIVED_MINUS_DISTRIBUTED,
            box_currency_value="USD",
        )
        assert result["total_cash"] == 0.0
        assert result["total_received"] == 0.0
        assert result["total_distributed"] == 0.0
