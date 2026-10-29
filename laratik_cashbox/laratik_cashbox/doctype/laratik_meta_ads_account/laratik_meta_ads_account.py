import re

import frappe
from frappe.model.document import Document


class LaraTikMetaAdsAccount(Document):
    def validate(self):
        if not re.fullmatch(r"act_\d+", str(self.external_account_id or "")):
            frappe.throw("Meta Ad Account ID must be an act_ ID returned by Meta.")
        if self.company:
            frappe.get_doc("Company", self.company).check_permission("read")
        if self.connection:
            connection = frappe.get_doc("LaraTik Meta Ads Connection", self.connection)
            connection.check_permission("read")
            if connection.company != self.company:
                frappe.throw("The Meta connection belongs to a different Company.")
        if self.billing_profile:
            profile = frappe.get_doc("LaraTik Meta Ads Billing Profile", self.billing_profile)
            profile.check_permission("read")
            if profile.company != self.company:
                frappe.throw("The billing profile belongs to a different Company.")
