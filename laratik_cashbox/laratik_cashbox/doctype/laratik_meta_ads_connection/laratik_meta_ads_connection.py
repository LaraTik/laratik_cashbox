import re

import frappe
from frappe.model.document import Document


class LaraTikMetaAdsConnection(Document):
    def validate(self):
        if not re.fullmatch(r"v\d+\.\d+", str(self.graph_api_version or "")):
            frappe.throw("Graph API version must look like v26.0.")
        if self.company:
            frappe.get_doc("Company", self.company).check_permission("read")
