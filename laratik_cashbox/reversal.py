"""The REVERSAL rule — what may be undone, and what the undo looks like.

─── WHY THIS EXISTS ──────────────────────────────────────────────────────────

A cancellation and a reversal are DIFFERENT facts, and the acceptance item
that asks for "reversals AND cancellations" asks for both because only one of
them used to exist.

**A cancellation** says: *this never happened*. The row is excluded from every
total, and the box's cash is what it would have been without the row.

**A reversal** says: *this happened, and here is the opposite leg*. The row
stays, the opposite leg is appended, and the two cancel each other out.

The application could do the second thing only by doing the first: a movement
that had already counted (`Completed`, `Settled`) could be switched to
`Cancelled`, and the rollup drops it — so the box's cash rose by the row's
amount, with no leg anywhere and nothing in the ledger saying why. A transfer
was the worst case, because the money had physically moved to ANOTHER box
(ADR-177).

So the rule this module writes down is:

* A row whose money NEVER moved may be CANCELLED. It did not count, so
  removing it changes no figure.
* A row whose money DID move may not be cancelled. It may be REVERSED, which
  appends the opposite leg.
* `Cancelled` and `Reversed` are terminal: no transition out of them.

The same rule is what ERPNext applies to a submitted document. You do not
cancel a posted journal entry; you post a reversing entry against it.

─── WHY IT IS A PURE MODULE ──────────────────────────────────────────────────

The same reason ``rollup.py``, ``currency_policy.py`` and ``idempotency.py``
are pure: the rule has to be TESTABLE without a site. This one is a row in
and an answer out, and ``laratik_cashbox/tests/test_reversal.py`` exercises it
without ERPNext.

The controller turns a refusal into an error; this module only says whether
there is one.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# ─── THE STATUS VOCABULARY ────────────────────────────────────────────────────

#: The status of a row that happened AND was undone by a compensating leg.
#:
#: It is NOT `Cancelled`. A cancelled row never counted; a reversed row DID
#: count, and the leg appended against it is what takes the count back out.
#: Both rows stay in the ledger, and the pair nets to zero.
STATUS_REVERSED = "Reversed"

#: The statuses whose rows COUNT towards the box's cash measure.
#:
#: `Reversed` is in this set because the money DID move: the original leg is
#: still there, and the reversal leg is what offsets it. A row that had never
#: counted cannot be reversed — there is nothing to take back.
COUNTED_STATUSES = ("Completed", "Settled", STATUS_REVERSED)

#: The statuses whose rows have NOT counted: the money never moved.
#:
#: `Pending` is an obligation, and `Cancelled` never happened at all. Neither
#: is in any total, so neither can be "taken back" — there is nothing there.
UNCOUNTED_STATUSES = ("Pending", "Cancelled")

#: Statuses with no way out. A row that was cancelled is cancelled, and a row
#: that was reversed has its leg; neither can be quietly rewritten into a
#: different fact, because that is how a balance changes without a trace.
TERMINAL_STATUSES = ("Cancelled", STATUS_REVERSED)

#: Every status the entry's `status` Select allows.
ENTRY_STATUSES = ("Pending", "Completed", "Settled", "Cancelled", STATUS_REVERSED)

#: The two directions a movement can go.
DIRECTION_IN = "In"
DIRECTION_OUT = "Out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)

#: A row that must not count towards anything.
#:
#: A rejected entry never happened. Counting it would put a figure in the
#: database that the application's own screens refuse to show.
REJECTED_APPROVAL_STATUS = "Rejected"

# ─── THE REFUSAL CODES ───────────────────────────────────────────────────────

#: The change is allowed.
ALLOWED = "ALLOWED"

#: The row's status is terminal, so no transition is possible.
REFUSED_TERMINAL = "REFUSED_TERMINAL"

#: The row's money moved, so it may not stop counting.
#:
#: Both `Pending` and `Cancelled` are OUTSIDE the counted set, and both would
#: remove the row's amount from the cash measure — `Pending` says the money
#: has not moved, and `Cancelled` says it never did. So both are refused for a
#: row whose money DID move, and the correction is a REVERSAL.
REFUSED_MONEY_ALREADY_MOVED = "REFUSED_MONEY_ALREADY_MOVED"

#: `Reversed` is not a status a caller sets. It is what the reversal path
#: writes, together with the leg. A caller that could set it alone would
#: remove a movement from the cash measure with no leg — the exact defect
#: this module exists to close.
REFUSED_REVERSED_IS_SET_BY_THE_REVERSAL = "REFUSED_REVERSED_IS_SET_BY_THE_REVERSAL"

#: A status the vocabulary does not contain.
REFUSED_UNKNOWN_STATUS = "REFUSED_UNKNOWN_STATUS"

#: The row did not count, so there is nothing to reverse.
REFUSED_NOTHING_TO_REVERSE = "REFUSED_NOTHING_TO_REVERSE"

#: The row already has its leg.
REFUSED_ALREADY_REVERSED = "REFUSED_ALREADY_REVERSED"

#: The row has no name, so a leg cannot point at it.
REFUSED_NO_NAME = "REFUSED_NO_NAME"

#: The row's direction is not one of the two, so its opposite cannot be
#: computed. An unclassifiable movement cannot be undone into a classifiable
#: one.
REFUSED_UNKNOWN_DIRECTION = "REFUSED_UNKNOWN_DIRECTION"


def _status(entry: Mapping[str, Any]) -> str:
    return str(entry.get("status") or "").strip()


def _direction(entry: Mapping[str, Any]) -> str:
    return str(entry.get("direction") or "").strip()


def _name(entry: Mapping[str, Any]) -> str:
    return str(entry.get("name") or "").strip()


def status_counts(entry: Mapping[str, Any]) -> bool:
    """Has this row's amount counted towards the box's cash measure?

    A `Pending` row is an obligation and a `Cancelled` row never happened, so
    neither is in any total. A `Completed`, `Settled` or `Reversed` row DID
    move money, so it counts — and a reversal leg is what offsets it.

    A REJECTED row never happened either, whatever its status says: the
    rollup drops it before the amount is even read. So it does not count,
    and it has nothing to reverse.
    """
    if str(entry.get("approval_status") or "").strip() == REJECTED_APPROVAL_STATUS:
        return False
    return _status(entry) in COUNTED_STATUSES


def status_change_is_allowed(
    entry: Mapping[str, Any], target: str
) -> tuple[bool, str]:
    """May this row move to `target`?

    Returns ``(allowed, reason)``. The reason is a stable code, not a
    sentence: the controller turns it into an error, the application turns it
    into a localized one, and a test asserts on the code.

    The rule, in the order it is applied:

    1. A status outside the vocabulary is refused. An unknown status is not a
       fact about the money; it is a typo, and a typo must not move a balance.
    2. `Reversed` cannot be set here. It is written by the reversal path,
       together with the leg that justifies it.
    3. `Cancelled` and `Reversed` are terminal.
    4. A row whose money MOVED may not stop counting. `Pending` and
       `Cancelled` are both outside the counted set, and both would remove the
       row's amount from the cash measure — so both are refused, and the
       correction is a REVERSAL, which appends the opposite leg.

    The row is the input rather than two status strings, because "did the
    money move" is a fact about the ROW and not about its status alone: a
    REJECTED row never counted, whatever its status says.
    """
    current = _status(entry)
    target = str(target or "").strip()

    if current not in ENTRY_STATUSES or target not in ENTRY_STATUSES:
        return False, REFUSED_UNKNOWN_STATUS

    if target == STATUS_REVERSED:
        return False, REFUSED_REVERSED_IS_SET_BY_THE_REVERSAL

    if current in TERMINAL_STATUSES:
        return False, REFUSED_TERMINAL

    if status_counts(entry) and target not in COUNTED_STATUSES:
        return False, REFUSED_MONEY_ALREADY_MOVED

    return True, ALLOWED


def already_reversed(
    entry: Mapping[str, Any], siblings: Iterable[Mapping[str, Any]] | None = None
) -> bool:
    """Does this row already have its leg?

    The leg names its original in `reversal_of`, so the question is answerable
    from the box's own rows — with no read of the audit log and no read of
    the GL.
    """
    name = _name(entry)
    if not name:
        return False
    for row in siblings or []:
        if str(row.get("reversal_of") or "").strip() == name:
            return True
    return False


def reversal_is_allowed(
    entry: Mapping[str, Any], siblings: Iterable[Mapping[str, Any]] | None = None
) -> tuple[bool, str]:
    """May this row be reversed?

    A row may be reversed when:

    * its money MOVED — it counted towards the cash measure, so there is
      something to take back. A `Pending` row never moved and a `Cancelled`
      row never happened; cancelling one of those is the right operation, and
      reversing one would post a leg against money that never left.
    * it has no leg yet. Two legs against one original would double the
      correction.
    * it has a name — a leg that cannot name what it reverses is a leg
      nobody can trace.
    * its direction is one of the two, so its opposite can be computed.
    """
    if not status_counts(entry):
        return False, REFUSED_NOTHING_TO_REVERSE

    if not _name(entry):
        return False, REFUSED_NO_NAME

    if _direction(entry) not in DIRECTIONS:
        return False, REFUSED_UNKNOWN_DIRECTION

    if already_reversed(entry, siblings):
        return False, REFUSED_ALREADY_REVERSED

    return True, ALLOWED


def build_reversal_leg(
    entry: Mapping[str, Any],
    *,
    entry_date: str,
    idempotency_key: str,
    note: str | None = None,
) -> dict[str, Any]:
    """The compensating leg for `entry`.

    Same amount, same currency, OPPOSITE direction, and a `reversal_of` that
    names the row it undoes. The leg is an ordinary movement: it is counted
    by the rollup exactly as the row it reverses was, which is why the pair
    nets to zero without the rollup knowing anything about reversals.

    ─── WHAT THE LEG INHERITS, AND WHY ───────────────────────────────────────

    `category`, `variance_type`, `other_box` and `counterparty` are copied
    from the original, and every one of them is copied for a REASON:

    * `variance_type` — preserve the explanation while the reversal's
      opposite direction offsets the actual cash movement.
    * `other_box` — this is what makes a row a TRANSFER. A leg without it
      would be a receipt or a distribution, so a reversed transfer would
      leave the box's transfer totals wrong.
    * `category` — the leg is the same KIND of fact, undone.
    * `counterparty` — the leg is against the same party.

    `money_flow` is NOT copied. It says between whom the money moved, and
    the leg moves the other way; carrying the original's value over would
    state a direction the row does not have. The leg leaves it unset, which
    also keeps it out of a money-flow filter — a correction is not a client
    movement, and a filter that claimed it was would be lying.
    """
    direction = _direction(entry)
    opposite = DIRECTION_OUT if direction == DIRECTION_IN else DIRECTION_IN

    leg: dict[str, Any] = {
        "entry_date": entry_date,
        "direction": opposite,
        "amount": entry.get("amount"),
        "currency": entry.get("currency"),
        "category": entry.get("category"),
        "status": "Completed",
        "approval_status": "Not Required",
        "reversal_of": _name(entry),
        "idempotency_key": idempotency_key,
    }

    for field in ("variance_type", "other_box", "counterparty", "funding_source"):
        value = entry.get(field)
        if value not in (None, ""):
            leg[field] = value

    if note:
        leg["notes"] = note

    return leg


def duplicate_reversal_targets(
    entries: Iterable[Mapping[str, Any]] | None,
) -> list[tuple[str, str]]:
    """The originals this box would reverse TWICE, with the first leg named.

    Each pair is ``(the original's name, the name of the FIRST leg that
    reverses it)``. A box may carry one leg per original: two legs would
    undo the same movement twice, and the box's cash would be wrong by the
    row's amount.
    """
    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []

    for row in entries or []:
        target = str(row.get("reversal_of") or "").strip()
        if not target:
            continue
        if target in seen:
            duplicates.append((target, seen[target]))
            continue
        seen[target] = _name(row) or "(a row with no name yet)"

    return duplicates


def orphan_reversal_legs(
    entries: Iterable[Mapping[str, Any]] | None,
) -> list[str]:
    """The legs that name an original this box does not carry.

    A leg whose `reversal_of` points at nothing is a correction of an
    unknown movement: the box's cash would move by the leg's amount with no
    row to take it back out of.
    """
    rows = list(entries or [])
    names = {_name(row) for row in rows if _name(row)}
    orphans: list[str] = []

    for row in rows:
        target = str(row.get("reversal_of") or "").strip()
        if not target:
            continue
        if target not in names:
            orphans.append(_name(row) or "(a row with no name yet)")

    return orphans


def unjustified_reversed_rows(
    entries: Iterable[Mapping[str, Any]] | None,
) -> list[str]:
    """The rows marked `Reversed` that no leg reverses.

    The mark and the leg are ONE fact: a row that says "reversed" with no
    leg against it is a movement that has been taken out of the cash measure
    with nothing to take it back out of — which is the silent deletion this
    rule exists to prevent, wearing a different name.

    The two are written together, so this cannot fire on a box that was
    saved with only one of them.
    """
    rows = list(entries or [])
    reversed_targets = {
        str(row.get("reversal_of") or "").strip()
        for row in rows
        if str(row.get("reversal_of") or "").strip()
    }
    unjustified: list[str] = []

    for row in rows:
        if _status(row) != STATUS_REVERSED:
            continue
        name = _name(row)
        if not name:
            continue
        if name not in reversed_targets:
            unjustified.append(name)

    return unjustified
