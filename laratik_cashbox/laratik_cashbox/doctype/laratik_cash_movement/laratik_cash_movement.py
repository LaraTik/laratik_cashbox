"""Standalone custody movement with an ERPNext document lifecycle."""

from decimal import Decimal, InvalidOperation

import frappe
from frappe.model.document import Document


# Standard ERPNext accounting documents that can carry submitted accounting
# evidence for a custody event. Restrict the Dynamic Link before querying it so
# an arbitrary/custom DocType cannot be presented as accounting authority.
ACCOUNTING_VOUCHER_DOCTYPES = {
    "Journal Entry",
    "Payment Entry",
    "Sales Invoice",
    "Purchase Invoice",
    "Expense Claim",
    "Payroll Entry",
    "Employee Advance",
}

APPROVAL_PROTECTED_FIELDS = (
    "movement_date", "company", "source_cash_box", "destination_cash_box", "direction",
    "amount", "currency", "category", "money_flow", "funding_source", "counterparty_type", "counterparty",
    "counterparty_text", "variance_type", "variance_amount", "purpose", "notes",
    "accounting_voucher_type", "accounting_voucher",
)


class LaraTikCashMovement(Document):
    def validate(self):
        if not self.idempotency_key:
            frappe.throw("An idempotency key is required for every movement.")
        try:
            amount = Decimal(str(self.amount or 0))
            positive_amount = amount.is_finite() and amount > 0
        except (InvalidOperation, TypeError, ValueError):
            positive_amount = False
        if not positive_amount:
            frappe.throw("Movement amount must be greater than zero.")
        self._validate_currency_precision("amount", amount)
        if self.category == "Invoice Issued":
            frappe.throw("An issued invoice is not a physical cash movement; use the ERPNext invoice workflow.")
        try:
            variance = Decimal(str(self.variance_amount or 0))
        except (InvalidOperation, TypeError, ValueError):
            frappe.throw("Variance amount must be a valid decimal amount.")
        if not variance.is_finite():
            frappe.throw("Variance amount must be a finite decimal amount.")
        self._validate_currency_precision("variance_amount", variance)
        if variance < 0:
            frappe.throw("Variance amount must not be negative; use the variance type to show its direction.")
        if variance and self.variance_type not in ("Overpayment", "Underpayment"):
            frappe.throw("A nonzero variance requires Overpayment or Underpayment as its variance type.")
        if not variance and self.variance_type in ("Overpayment", "Underpayment"):
            frappe.throw("An overpayment or underpayment requires a nonzero variance amount.")
        if self.counterparty_type and self.counterparty_type not in ("Customer", "Supplier", "Employee"):
            frappe.throw("Known counterparties must link to Customer, Supplier, or Employee.")
        self._validate_box_scope()
        self._validate_counterparty()
        self._validate_transfer()
        self._validate_voucher()
        self._validate_reversal()
        self._validate_approved_values_unchanged()
        self._validate_approval_separation()

    def before_submit(self):
        if self.migration_state == "Unverified":
            frappe.throw("An unverified legacy mirror cannot be submitted or counted in complete totals.")
        if self.workflow_state != "Submitted":
            frappe.throw("A cash movement must pass the LaraTik Cash Movement Approval workflow before submission.")

    def on_submit(self):
        self._refresh_cash_boxes()

    def on_cancel(self):
        frappe.throw("A submitted cash movement cannot be cancelled. Create a linked reversal instead.")

    def on_update_after_submit(self):
        frappe.throw("Submitted cash movements are immutable. Create a linked reversal to correct one.")

    def _validate_currency_precision(self, fieldname, value):
        precision = self.precision(fieldname)
        if not isinstance(precision, int) or precision < 0:
            frappe.throw(f"Cannot determine ERPNext precision for {fieldname}.")
        quantum = Decimal(1).scaleb(-precision)
        try:
            rounded = value.quantize(quantum)
        except InvalidOperation:
            frappe.throw(f"{fieldname} exceeds the supported ERPNext precision.")
        if value != rounded:
            frappe.throw(f"{fieldname} exceeds ERPNext precision of {precision} decimal places.")

    def _box(self, name):
        return frappe.db.get_value(
            "LaraTik Cash Box", name, ["cash_account", "employee", "related_client"], as_dict=True
        )

    def _company_for_box(self, name):
        box = self._box(name)
        if not box or not box.cash_account:
            frappe.throw(f"Cash box {name} has no required cash account.")
        account = frappe.db.get_value("Account", box.cash_account, ["company", "account_currency"], as_dict=True)
        if not account or not account.company:
            frappe.throw(f"Cash box {name} has no company on its linked Account.")
        company_currency = frappe.db.get_value("Company", account.company, "default_currency")
        currency = account.account_currency or company_currency
        return box, account.company, currency

    def _validate_box_scope(self):
        source, company, currency = self._company_for_box(self.source_cash_box)
        if not self.company:
            self.company = company
        elif self.company != company:
            frappe.throw("Movement company must match the company derived from the source cash box account.")
        if self.migration_state != "Unverified":
            if not currency:
                frappe.throw("Currency cannot be determined for the source cash box account.")
            if self.currency != currency:
                frappe.throw("Movement currency must match the source cash box account currency.")
        if self.destination_cash_box:
            _, dest_company, dest_currency = self._company_for_box(self.destination_cash_box)
            if dest_company != company:
                frappe.throw("Intercompany transfers must use ERPNext intercompany accounting.")
            if self.migration_state != "Unverified" and dest_currency != currency:
                frappe.throw("Cash box transfers must use the same currency on both sides.")

    def _validate_transfer(self):
        if self.destination_cash_box == self.source_cash_box:
            frappe.throw("A transfer must identify two different cash boxes.")
        if self.destination_cash_box and self.direction != "Out":
            frappe.throw("A two-sided transfer records an outgoing source movement; the destination side is derived.")

    def _validate_counterparty(self):
        if bool(self.counterparty_type) != bool(self.counterparty):
            frappe.throw("Counterparty type and party must be supplied together.")
        if self.counterparty and self.counterparty_text:
            frappe.throw("Use either a linked ERPNext party or unmatched counterparty text, not both.")
        if not self.counterparty_type:
            raw_text = (self.counterparty_text or "").strip()
            if raw_text:
                for doctype, display_field in (
                    ("Customer", "customer_name"),
                    ("Supplier", "supplier_name"),
                    ("Employee", "employee_name"),
                ):
                    if frappe.db.exists(doctype, raw_text) or frappe.db.exists(
                        doctype, {display_field: raw_text}
                    ):
                        frappe.throw(
                            "This counterparty matches an ERPNext party master; select the linked party instead of entering free text."
                        )
            return
        if not frappe.db.exists(self.counterparty_type, self.counterparty):
            frappe.throw("The linked ERPNext counterparty does not exist.")
        if self.counterparty_type == "Employee":
            employee_company = frappe.db.get_value("Employee", self.counterparty, "company")
            if employee_company and employee_company != self.company:
                frappe.throw("Employee counterparty must belong to the cash movement company.")

    def _validate_approval_separation(self):
        """Enforce creator/approver separation, including Frappe Administrator."""
        if getattr(getattr(frappe, "flags", None), "in_cash_movement_migration", False):
            return
        previous = getattr(self, "_doc_before_save", None)
        if not previous:
            return
        previous_state = previous.get("workflow_state") if isinstance(previous, dict) else getattr(previous, "workflow_state", None)
        if (previous_state in ("Draft", "Pending Approval")
                and self.workflow_state in ("Approved", "Rejected")
                and getattr(getattr(frappe, "session", None), "user", None) == self.owner):
            frappe.throw("The movement creator cannot approve or reject their own cash movement.")

    def _validate_approved_values_unchanged(self):
        previous = getattr(self, "_doc_before_save", None)
        if not previous:
            return
        previous_state = previous.get("workflow_state") if isinstance(previous, dict) else getattr(previous, "workflow_state", None)
        if previous_state not in ("Pending Approval", "Approved", "Rejected"):
            return
        for fieldname in APPROVAL_PROTECTED_FIELDS:
            if hasattr(self, "has_value_changed"):
                changed = self.has_value_changed(fieldname)
            else:
                old_value = previous.get(fieldname) if isinstance(previous, dict) else getattr(previous, fieldname, None)
                changed = old_value != getattr(self, fieldname, None)
            if changed:
                frappe.throw(
                    "Movement details are locked after approval is requested. Return it to Draft before making changes."
                )

    def _validate_voucher(self):
        if bool(self.accounting_voucher_type) != bool(self.accounting_voucher):
            frappe.throw("Accounting voucher type and voucher must be supplied together.")
        if self.accounting_voucher_type:
            if self.accounting_voucher_type not in ACCOUNTING_VOUCHER_DOCTYPES:
                frappe.throw("Accounting evidence must reference a supported standard ERPNext accounting voucher.")
            voucher = frappe.db.get_value(
                self.accounting_voucher_type, self.accounting_voucher, ["docstatus", "company"], as_dict=True
            )
            if not voucher:
                frappe.throw("The linked ERPNext accounting voucher does not exist.")
            if voucher.docstatus != 1:
                frappe.throw("Accounting evidence must reference a submitted ERPNext voucher.")
            if voucher.company and voucher.company != self.company:
                frappe.throw("Accounting voucher company must match the cash movement company.")

    def _validate_reversal(self):
        if not self.reversal_of:
            return
        original = frappe.get_doc("LaraTik Cash Movement", self.reversal_of)
        if original.docstatus != 1:
            frappe.throw("Only a submitted movement can be reversed.")
        if original.company != self.company or original.currency != self.currency:
            frappe.throw("A reversal must retain the original company and currency.")
        if original.destination_cash_box:
            if (self.amount != original.amount or
                    self.source_cash_box != original.destination_cash_box or
                    self.destination_cash_box != original.source_cash_box or
                    self.direction != "Out" or original.direction != "Out"):
                frappe.throw("A transfer reversal must reverse the source and destination boxes together.")
        else:
            if original.amount != self.amount or original.source_cash_box != self.source_cash_box:
                frappe.throw("A reversal must match the original amount and source cash box.")
            expected = "In" if original.direction == "Out" else "Out"
            if self.direction != expected:
                frappe.throw("A reversal must use the opposite direction.")

    def _refresh_cash_boxes(self):
        names = {self.source_cash_box, self.destination_cash_box}
        for name in filter(None, names):
            frappe.enqueue(
                "laratik_cashbox.laratik_cashbox.doctype.laratik_cash_movement.laratik_cash_movement.refresh_cash_box",
                cash_box=name,
                enqueue_after_commit=True,
            )


def refresh_cash_box(cash_box):
    """Refresh the transitional operational projection from submitted movements."""
    doc = frappe.get_doc("LaraTik Cash Box", cash_box)
    # Until the UI/report cutover is complete, legacy child-table projections
    # remain untouched. A complete projection switches only with the resumable
    # mirror migration and reconciliation gate.
    if hasattr(doc, "apply_rollup"):
        doc.apply_rollup()
