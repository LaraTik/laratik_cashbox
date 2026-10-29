from decimal import Decimal, InvalidOperation

import frappe
from frappe.model.document import Document


class LaraTikMetaAdsBillingProfile(Document):
    def validate(self):
        if self.company:
            frappe.get_doc("Company", self.company).check_permission("read")
        if self.customer:
            customer = frappe.get_doc("Customer", self.customer)
            customer.check_permission("read")
            if customer.disabled:
                frappe.throw("Choose an enabled Customer.")
        if self.invoice_currency:
            enabled = frappe.db.get_value("Currency", self.invoice_currency, "enabled")
            if not enabled:
                frappe.throw("Choose an enabled invoice currency.")
        try:
            fee = Decimal(str(self.fee_percent or "0"))
        except (InvalidOperation, TypeError, ValueError):
            frappe.throw("Service fee must be a valid percentage.")
        if not fee.is_finite() or fee < 0 or fee > 100:
            frappe.throw("Service fee must be between 0% and 100%.")
