import re
from decimal import Decimal

import frappe
from frappe.model.document import Document

DECIMAL = re.compile(r"^\d+(?:\.\d+)?$")
RATE = re.compile(r"^\d+(?:\.\d+)?$")


class LaraTikMetaAdsInvoiceLine(Document):
    """One chargeable item: its source, its basis, its fee and its conversion.

    ─── WHY THIS RECORD CANNOT BE EDITED ────────────────────────────────────────

    A line is the traceable source of one figure on an invoice. If it could be
    edited, the invoice and the spend behind it would stop agreeing, and
    nothing would say when or why they stopped agreeing. The line is written
    once, with the batch it belongs to, and it stays.

    ─── WHY THE RATE MAY NOT BE ZERO ─────────────────────────────────────────────

    A missing exchange rate is `""`, never `0`. A line that says `0` would say
    "these two currencies are equal", which is a claim about money that nobody
    made and that nobody checked. The application withholds the amount instead,
    and this is where that promise is kept.
    """

    def validate(self):
        if not self.is_new():
            frappe.throw(
                "An invoice line is the traceable source of an invoiced figure and cannot be edited."
            )
        self._validate_amounts()
        self._validate_rate()
        self._validate_batch_company()

    def on_trash(self):
        frappe.throw("Invoice lines are retained as audit evidence.")

    def _validate_amounts(self):
        for field in ("basis_amount", "fee_amount", "charge_amount"):
            value = str(getattr(self, field) or "")
            if value and not DECIMAL.match(value):
                frappe.throw(
                    "The invoice line {0} must be a non-negative decimal amount.".format(field)
                )

    def _validate_rate(self):
        value = str(self.exchange_rate or "")
        if not value:
            return
        if not RATE.match(value) or Decimal(value) <= 0:
            frappe.throw(
                "The invoice line exchange rate must be a positive decimal. "
                "A missing rate is left empty, never zero."
            )

    def _validate_batch_company(self):
        if not self.invoice_batch or not self.company:
            return
        batch_company = frappe.db.get_value(
            "LaraTik Meta Ads Invoice Batch", self.invoice_batch, "company"
        )
        if batch_company and batch_company != self.company:
            frappe.throw("The invoice line belongs to a batch in another Company.")
