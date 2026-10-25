"""A small, non-financial draft store for the first-run setup wizard."""

import json

from frappe.model.document import Document

from laratik_cashbox.setup_state import normalize_setup_state


class LaraTikSetupState(Document):
	"""Keep resumable company and account-mapping inputs outside the ledger."""

	def validate(self):
		state = normalize_setup_state(self.setup_json or "{}")
		self.setup_json = json.dumps(state, ensure_ascii=False, sort_keys=True)
		company = state["company"]
		self.status = (
			"In Progress"
			if any(company.values()) or any(state["account_mappings"].values()) or state["tax_review_acknowledged"]
			else "Not Started"
		)

