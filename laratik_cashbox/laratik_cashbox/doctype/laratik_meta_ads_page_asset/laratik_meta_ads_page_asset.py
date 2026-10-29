import frappe
from frappe.model.document import Document


class LaraTikMetaAdsPageAsset(Document):
    def validate(self):
        if self.served_customer:
            customer = frappe.get_doc("Customer", self.served_customer)
            customer.check_permission("read")
