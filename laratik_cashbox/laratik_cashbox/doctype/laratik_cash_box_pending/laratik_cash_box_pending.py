"""`LaraTik Cash Box Pending` — one outstanding obligation of a box.

A party is a ROW. The previous model had one column per named person on the
parent (`pending_mohamad`, `pending_laratik`, `pending_hussein`,
`pending_others`), matched by substring against a free-text counterparty, so
adding a fifth person was a schema change (ADR-169).

The same reasoning applies here: a pending row is inserted, updated or
deleted, and the parent's derived columns move with it.
"""

import frappe
from frappe.model.document import Document


class LaraTikCashBoxPending(Document):
	"""One outstanding obligation."""

	def after_insert(self):
		self._roll_up_parent()

	def on_update(self):
		self._roll_up_parent()

	def after_delete(self):
		self._roll_up_parent()

	def _roll_up_parent(self):
		"""Recompute the parent box's derived columns.

		`apply_rollup` writes with `db_set`, which does NOT re-enter the
		parent's document lifecycle — so this cannot loop back into itself.
		"""
		if not self.get("parent") or self.parenttype != "LaraTik Cash Box":
			return
		frappe.get_doc("LaraTik Cash Box", self.parent).apply_rollup()
