import frappe
from frappe.model.document import Document


class LaraTikMetaAdsSync(Document):
    def validate(self):
        previous = self.get_doc_before_save()
        if not previous:
            return

        if previous.status != "Pending" or self.status not in {"Complete", "Failed"}:
            frappe.throw("A Meta Ads sync can only move once from Pending to a final status.")

        fixed_fields = (
            "company", "ad_account", "external_account_id", "from_date", "to_date",
            "fetched_at", "source",
        )
        if any(self.get(field) != previous.get(field) for field in fixed_fields):
            frappe.throw("The identity and period of a Meta Ads sync run cannot be changed.")
        if previous.ad_insights_status != "Not Fetched":
            frappe.throw("Ad-level Meta Insights status cannot be changed after a sync is finalized.")
        if self.ad_insights_status not in {"Not Fetched", "Complete", "Failed"}:
            frappe.throw("Ad-level Meta Insights must have a final status.")
