from frappe.model.document import Document


class LaraTikMetaAdsPayment(Document):
	def validate(self):
		if not self.is_new():
			import frappe
			frappe.throw("Meta payment rows are immutable. Import a corrected source report for reconciliation.")

	def on_trash(self):
		import frappe
		frappe.throw("Meta payment rows are retained as audit evidence.")
