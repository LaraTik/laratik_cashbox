"""Backfill the `LaraTik Cash Box` balance columns that are still empty.

─── WHY THIS PATCH EXISTS, AND WHY IT FILLS ONLY WHAT IS MISSING ─────────────

The balance columns were maintained by a Frappe Server Script on the
instance. That script was not part of this repository and was not installed by
`bench install-app`, so on a fresh install — or on any instance that never
had it — the columns stayed at their defaults and the application showed
`$0.00` while the real rows sat in `LaraTik Cash Box.entries`.

`LaraTik Cash Box.apply_rollup` now computes them, so a box that is written from now
on has a correct balance. This patch handles the boxes that were written
BEFORE that, and nothing else.

**IT FILLS ONLY EMPTY COLUMNS.** A box whose columns already hold figures
keeps every one of them. An instance running a working Server Script, whose
figures are correct and whose operator has not yet removed that script, must
not have its numbers silently replaced by a different computation on migrate.

That matters because the two computations are not identical in every respect:
the instance's script knew which box was the MAIN cash box from its own
configuration, and this app's schema carries that as the `balance_rule`
field, which an existing box will still have at its default. The default is
`Outstanding`, which is right for every box except the main one. So the
backfill leaves a populated main box alone, and the operator sets
`balance_rule` on it — at which point the rollup takes over and computes the
figure the main box's rule calls for.

─── WHY IT RUNS AFTER THE VERIFICATION PATCHES ───────────────────────────────

`bench migrate` syncs the DocType JSON into the site, which is what ADDS the
`balance_rule` field. The verification patches then check the live schema
against the repository's JSON, and this patch runs after them, so it can rely
on the field existing.

─── WHAT IT DOES NOT DO ──────────────────────────────────────────────────────

It does not remove the instance's Server Script. If one is running, it will
keep writing the same columns, and the two must be reconciled by the
operator: set `balance_rule` correctly, then remove the Server Script so
there is one writer. See `docs/DEPLOYMENT.md`.
"""

import frappe

from laratik_cashbox.laratik_cashbox.doctype.laratik_cash_box.rollup import (
    compute_rollup,
)

#: The columns this patch fills. `current_balance` is in here because it is
#: the one the application reads 35 times.
BACKFILL_FIELDS = (
    "current_balance",
    "total_cash",
    "total_received",
    "total_distributed",
    "total_outstanding",
    "last_activity",
)


def _is_empty(value) -> bool:
    """Is this column one nobody has written a figure into?

    A `0` is NOT empty. A box whose real balance is zero has a figure of zero
    in it, and overwriting that with a computed zero would be a change nobody
    asked for. Only `None` and the empty string count as empty.
    """
    return value is None or value == ""


def backfill_laratik_cash_box_balances() -> None:
    """Fill the empty balance columns on every `LaraTik Cash Box`."""
    boxes = frappe.get_all(
        "LaraTik Cash Box",
        # Child Table fields live in their own tables; they are available on
        # the loaded document, not as columns in the parent list query.
        fields=["name"],
        limit=0,
    )

    filled = 0
    skipped = 0
    for box in boxes:
        doc = frappe.get_doc("LaraTik Cash Box", box.name)
        existing = {field: doc.get(field) for field in BACKFILL_FIELDS}
        if not any(_is_empty(value) for value in existing.values()):
            skipped += 1
            continue

        computed = compute_rollup(
            doc.get("entries") or [],
            doc.get("pending_items") or [],
            rule=doc.get("balance_rule") or "Outstanding",
            box_currency_value=doc._box_currency(),
        )
        values = {
            field: computed.get(field)
            for field in BACKFILL_FIELDS
            if _is_empty(existing.get(field))
        }
        if not values:
            skipped += 1
            continue
        # `db_set` and not `doc.save()`: a save would re-enter the document
        # lifecycle, and `on_update` would run the rollup over the whole box.
        doc.db_set(values, update_modified=False)
        filled += 1

    if filled:
        frappe.logger().info(
            "laratik_cashbox: backfilled the balance columns on %s cash box(es); "
            "%s already had figures and were left alone.",
            filled,
            skipped,
        )


def execute() -> None:
    """Frappe's patch runner invokes the module-level execute function."""
    backfill_laratik_cash_box_balances()
