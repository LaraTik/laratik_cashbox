import re

import frappe
from frappe.model.document import Document


class LaraTikMetaAdsSpend(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("Fetched Meta Ads spend snapshots cannot be edited.")
        if not re.fullmatch(r"\d+(?:\.\d+)?", str(self.amount or "")):
            frappe.throw("Meta Insights spend must be a non-negative decimal amount.")
        if self.level == "Account" and self.attribution_status != "Not Applicable":
            frappe.throw("Account-level spend cannot carry Page attribution.")
        if self.level == "Ad" and self.attribution_status == "Not Applicable":
            frappe.throw("Ad-level spend must state its Page attribution status.")
