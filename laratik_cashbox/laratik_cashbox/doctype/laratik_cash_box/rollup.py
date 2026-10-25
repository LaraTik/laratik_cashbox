"""The balance rollup — the computation, with no Frappe in it.

─── WHY THIS FILE IS SEPARATE, AND WHY IT HAS NO `import frappe` ──────────────

The rollup is the thing that gives the `LaraTik Cash Box` balance columns an owner.
The computation itself is arithmetic over child rows, and arithmetic over
child rows does not need a database, a request, or a document. So it lives
here, alone, and it is tested here.

That is not tidiness. It is the reason the app has a Python test at all: the
Frappe app has never had a test, because everything in it needed a live site.
A pure module does not.

─── WHAT CHANGED, AND WHY (ADR-169) ───────────────────────────────────────────

There used to be TWO child tables — transactions and transfers — and THREE
frozensets of `transaction_type` values that decided whether a row was a
receipt, a distribution or an obligation. The application mirrored those
frozensets in TypeScript, and a parity test checked the two lists agreed.

**The frozensets are gone.** A row now says what it is in its own fields:
`direction` (`In` | `Out`), `category`, and `money_flow`. So there is nothing
left to keep in step, and the parity test had nothing to compare. A new kind
of money movement is now a new option in a Select — it does not need a Python
change and a patch.

**The transfers table is gone.** A transfer is an entry whose `other_box` is
set: it moves custody between two boxes, which is a counterparty fact and not
a different kind of row. One ledger, assembled from one place.

**The four `pending_*` columns are gone.** They held one amount per named
person, matched by substring against a free-text counterparty, so adding a
fifth person was a schema change. What is owed is now
`LaraTik Cash Box Pending` — one row per obligation, and a party is a row.

─── WHAT IT COMPUTES, AND WHY EACH FIGURE IS DEFINED THIS WAY ─────────────────

Every definition below is taken from the application that READS these columns,
because a rollup that disagrees with its reader is worse than no rollup — the
reader would show a number the database does not believe.

`src/lib/erpnext/financials/balance.ts` (the header, which is the spec):

    Main box:  current_balance = total_received - total_distributed
    Other boxes: current_balance = total_outstanding
                where total_outstanding = what the pending roster says is owed

─── THE ONE THING THIS FILE CANNOT DECIDE FOR ITSELF ──────────────────────────

`current_balance` has TWO definitions, and which one applies depends on
whether the box is the company's main cash box. That used to be private
knowledge inside an unversioned Server Script on the instance, which is why
the columns had no owner and why a fresh install showed `$0.00`.

The rule is now a FIELD on the box (`balance_rule`), so the schema states it
and this file applies it. The default is `Outstanding`, which is what every
box except the main one uses, and therefore the only default that cannot
change an existing figure.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
from typing import Any, Iterable, Mapping

from laratik_cashbox.currency_policy import row_is_sumable

# ─── THE RULES ─────────────────────────────────────────────────────────────────

#: `current_balance = total_received - total_distributed`.
#:
#: The main cash box. Cash on hand, including money earmarked for somebody.
RULE_RECEIVED_MINUS_DISTRIBUTED = "Received minus distributed"

#: `current_balance = total_outstanding`.
#:
#: Every other box. Zero means fully settled.
RULE_OUTSTANDING = "Outstanding"

#: The default, and the only one that cannot change an existing figure.
DEFAULT_BALANCE_RULE = RULE_OUTSTANDING

BALANCE_RULES = (RULE_RECEIVED_MINUS_DISTRIBUTED, RULE_OUTSTANDING)

# ─── THE ENTRY AXES ────────────────────────────────────────────────────────────

#: The money came INTO this box.
DIRECTION_IN = "In"

#: The money went OUT of this box.
DIRECTION_OUT = "Out"

#: The money has NOT moved. The entry is an OBLIGATION.
#:
#: This is the reading that changed (ADR-169). Before, a row was classified by
#: its `transaction_type`, and the `status` field was never read by the rollup
#: at all — so a `Cash Received` the application's own screens showed as
#: PENDING counted towards `total_received`. The rollup and the screens
#: disagreed about what "pending" means, and the rollup was the one that
#: produced the number.
STATUS_PENDING = "Pending"

#: A pending row that has been settled. It is no longer owed, so it leaves the
#: obligation measure.
PENDING_ITEM_SETTLED = "Settled"

#: The entry did not happen. It is in no total.
#:
#: This is the vocabulary the TRANSFER table used to have on its own, and the
#: transaction table did not: one ledger has one status vocabulary, and it is
#: the union of the two.
STATUS_CANCELLED = "Cancelled"

#: A row that must not count towards anything.
#:
#: A rejected entry never happened. Counting it would put a figure in the
#: database that the application's own screens refuse to show.
REJECTED_APPROVAL_STATUS = "Rejected"

#: Variance is metadata about the difference from an expected amount.
#:
#: The amount remains actual money moved and is counted by its direction.
#:
#: The old model could only say this because `transaction_type` was the ONE
#: axis: a row was either incoming or it was a variance, and a variance could
#: not be both. Now `direction` says which way the money went and
#: `variance_type` annotates the reason but does not change movement direction.
VARIANCE_TYPES = ("Overpayment", "Underpayment")


ZERO = Decimal("0")


def _amount(row: Mapping[str, Any]) -> Decimal:
    """The amount of a child row, as a number.

    A row whose amount is missing, blank or unparseable contributes NOTHING,
    and the rollup records that it could not be read. A missing amount is not
    a zero — that is the same rule the application follows, and the reason it
    renders "unreadable" instead of `$0.00`.
    """
    raw = row.get("amount")
    if raw is None or raw == "":
        return ZERO
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO
    if not value.is_finite():
        return ZERO
    return value


def _is_readable(row: Mapping[str, Any]) -> bool:
    """Can this row's amount be read at all?"""
    raw = row.get("amount")
    if raw is None or raw == "":
        return False
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return value.is_finite()


def _counts(entry: Mapping[str, Any]) -> bool:
    """Should this entry count towards the rollup at all?

    Only completed physical movements count. Pending or rejected approvals,
    non-movement statuses, and unsubmitted standalone drafts are excluded.
    """
    if entry.get("standalone_docstatus") is not None and entry.get("standalone_docstatus") != 1:
        return False
    # An issued invoice is an accounting document/event, not cash custody.
    # It may be reported from ERPNext, but it cannot change a cashbox total.
    if entry.get("category") == "Invoice Issued":
        return False
    approval = entry.get("approval_status")
    if approval in ("Pending", REJECTED_APPROVAL_STATUS):
        return False
    return entry.get("status") in ("Completed", "Settled", "Reversed")


def _is_transfer(entry: Mapping[str, Any]) -> bool:
    """Is this entry a TRANSFER — a movement of custody between two boxes?

    A transfer is an entry whose `other_box` is set. It moves the balance
    without being a receipt or a distribution, which is why it is counted
    separately in the cash measure.
    """
    return bool(entry.get("other_box"))


def compute_rollup(
    entries: Iterable[Mapping[str, Any]],
    pending_items: Iterable[Mapping[str, Any]] = (),
    rule: str = DEFAULT_BALANCE_RULE,
    box_currency_value: str | None = None,
) -> dict[str, Any]:
    """Compute every derived figure on a `LaraTik Cash Box`.

    :param entries: legacy `LaraTik Cash Box Entry` rows plus submitted standalone
        movements projected into the same direction/currency shape by the controller.
    :param pending_items: the box's `LaraTik Cash Box Pending` child rows —
        what is OWED. One row per obligation, with the party on the row.
    :param rule: which `current_balance` definition applies. One of
        :data:`BALANCE_RULES`. An unknown rule falls back to
        :data:`DEFAULT_BALANCE_RULE`, because a wrong balance is worse than
        the conservative one.
    :param box_currency_value: the currency this box's amounts are in, or
        `None` when it is unknown. An unknown box denomination or a row in a
        DIFFERENT/unknown currency is refused and counted as unreadable, because a
        conversion needs a rate, a rate needs a date, and a wrong rate is a
        wrong number that looks right (ADR-170).
    :returns: a dict of the fieldnames the `LaraTik Cash Box` doctype declares,
        plus `unreadable_count` and `readable`, which are NOT fields — they
        are the rollup's own account of what it could and could not read.

    The transfers are counted as IN or OUT by their `direction`, and they move
    the balance without being receipts or distributions. They are the reason a
    box's cash can change without a receipt.
    """
    total_received = ZERO
    total_distributed = ZERO
    transfer_in = ZERO
    transfer_out = ZERO
    unreadable = 0
    last_activity: str | None = None
    for row in entries:
        if not _counts(row):
            continue
        if not _is_readable(row):
            unreadable += 1
            continue
        if not box_currency_value or not row_is_sumable(row.get("currency"), box_currency_value):
            # A row in another currency is REFUSED, not converted. It
            # contributes nothing, and it is counted as unreadable so the
            # controller warns and the application renders "unreadable"
            # rather than a total that quietly adds two currencies.
            unreadable += 1
            continue
        amount = _amount(row)
        direction = row.get("direction")

        # ─── THE OBLIGATION IS NOT A MOVEMENT ─────────────────────────────────
        #
        # A pending entry is something the company OWES, not something that
        # happened. It is in the pending roster, and it is in no movement
        # total. Putting it in one is how a pending cash receipt became
        # "received".
        #
        # Variance metadata does not erase the real amount; the direction
        # and transfer destination below classify the movement once.
        #
        # And an entry whose direction is neither `In` nor `Out` is in NEITHER
        # total: an unclassifiable row must not be counted as incoming by
        # default, because the direction says which way the money went and
        # guessing wrong is how a balance gets bigger than it is.
        if _is_transfer(row):
            if direction == DIRECTION_IN:
                transfer_in += amount
            elif direction == DIRECTION_OUT:
                transfer_out += amount
        elif direction == DIRECTION_IN:
            total_received += amount
        elif direction == DIRECTION_OUT:
            total_distributed += amount

        # ─── THE VARIANCE AND THE DATES ────────────────────────────────────────
        #
        # Variance metadata explains a difference from an expected amount.
        # `amount` is still the actual cash moved and the direction above
        # counts it once. Dates describe the movement independently.
        #
        # The dates on counted rows are movement dates, so they define the
        # last physical activity in the box.
        for field in ("entry_date", "posting_date"):
            value = row.get(field)
            if isinstance(value, str) and value:
                if last_activity is None or value > last_activity:
                    last_activity = value

    # ─── WHAT IS OWED ───────────────────────────────────────────────────────────
    #
    # The roster IS the obligation measure. A party is a row, so there is no
    # per-person column to keep in step with the rollup's idea of who owes
    # what — and a settled row leaves the total because it is settled, not
    # because a formula subtracted it.
    total_outstanding = ZERO
    pending_count = 0
    for item in pending_items:
        if item.get("status") == PENDING_ITEM_SETTLED:
            continue
        if not _is_readable(item):
            unreadable += 1
            continue
        if not box_currency_value or not row_is_sumable(item.get("currency"), box_currency_value):
            unreadable += 1
            continue
        total_outstanding += _amount(item)
        pending_count += 1

    # ─── THE OPERATIONAL CASH, ALWAYS, AND NAMED ──────────────────────────────
    #
    # The cash that actually moved through this box. It is computed for EVERY
    # box, whatever `balance_rule` says, and it has its OWN name (ADR-170).
    #
    # Why it needs its own name: `current_balance` is the RULE-SELECTED figure,
    # and ADR-167 schedules it for retirement when the general ledger becomes
    # the balance. If the operational cash lived only in `current_balance`, then
    # when that retirement landed the rollup's cash measure would lose its
    # home — and two different "cash" numbers would compete for one field name:
    # the operational cash the till actually moved, and the accounting cash the
    # GL says the account holds. They are different facts. Reconciling them is
    # F2, and the signal is `total_cash` against the GL balance of
    # `cash_account`, per box, per company, per currency.
    total_cash = total_received + transfer_in - (total_distributed + transfer_out)

    if rule == RULE_RECEIVED_MINUS_DISTRIBUTED:
        current_balance = total_cash
    else:
        current_balance = total_outstanding

    return {
        "current_balance": current_balance,
        "total_cash": total_cash,
        "total_received": total_received,
        "total_distributed": total_distributed,
        "total_outstanding": total_outstanding,
        "last_activity": last_activity,
        # Not a FIELD. The application reads the roster's rows; this is the
        # rollup's own account of what it could and could not read.
        "unreadable_count": unreadable,
        "readable": unreadable == 0,
        "pending_count": pending_count,
    }
