import re

import frappe
from frappe.model.document import Document

DECIMAL = re.compile(r"^\d+(?:\.\d+)?$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LaraTikMetaAdsInvoiceBatch(Document):
    """One invoiced period, and the figures it was invoiced on.

    ─── WHY THIS RECORD CANNOT BE EDITED ────────────────────────────────────────

    The batch exists for one reason: to pin the period, the policy and the
    figures a charge was created from, so that a later re-sync — which changes
    the spend — shows up as a DIFFERENCE instead of quietly rewriting an
    approved period. A batch that could be edited would not pin anything, and
    the divergence would be invisible again.

    The batch's `status` is the one field that moves, and it moves only
    because ERPNext says so: the invoice is submitted, cancelled or amended
    there, and the application records what ERPNext reports. That write is a
    database-level update and does not run this validation, so the two rules
    do not fight each other.

    The batch is also not deletable. It is the link between a Sales Invoice
    that exists in ERPNext and the spend that produced it, and deleting it
    would orphan the invoice instead of undoing it.
    """

    def validate(self):
        if not self.is_new():
            frappe.throw(
                "An invoice batch is a record of what was invoiced and cannot be edited. "
                "Amend or cancel the Sales Invoice in ERPNext; the status follows it."
            )
        self._validate_period()
        self._validate_totals()

    def on_trash(self):
        frappe.throw(
            "Invoice batches are retained as audit evidence. Cancel the Sales Invoice instead."
        )

    def _validate_period(self):
        for field in ("period_start", "period_end"):
            value = str(getattr(self, field) or "")
            if value and not DATE.match(value):
                frappe.throw("The invoice batch period must hold a YYYY-MM-DD date.")
        start = str(self.period_start or "")
        end = str(self.period_end or "")
        if start and end and start > end:
            frappe.throw("The invoice batch period ends before it starts.")

    def _validate_totals(self):
        for field in ("basis_total", "fee_total", "total"):
            value = str(getattr(self, field) or "")
            if value and not DECIMAL.match(value):
                frappe.throw(
                    "The invoice batch {0} must be a non-negative decimal amount.".format(field)
                )
