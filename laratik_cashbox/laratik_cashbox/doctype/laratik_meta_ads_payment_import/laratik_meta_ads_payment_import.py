from frappe.model.document import Document


class LaraTikMetaAdsPaymentImport(Document):
	def validate(self):
		if not self.is_new():
			import frappe
			frappe.throw("Meta payment import batches are immutable.")

	def on_trash(self):
		import frappe
		frappe.throw("Meta payment import batches are retained as audit evidence.")
