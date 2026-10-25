"""`LaraTik Cash Box Entry` — one movement against a box.

The only behaviour here is telling the parent that its balance changed. A child
row cannot be inserted, updated or deleted without the parent's derived
columns moving, or the box reports a balance that its own ledger contradicts.

A TRANSFER IS AN ENTRY, not a separate table: it is an entry whose `other_box`
is set. There used to be two child tables, and a balance had to be assembled
from two places (ADR-169).
"""

import frappe
from frappe.model.document import Document


class LaraTikCashBoxEntry(Document):
	"""One movement against a box."""

	def after_insert(self):
		self._roll_up_parent()

	def on_update(self):
		self._roll_up_parent()

	def after_delete(self):
		self._roll_up_parent()

	def _roll_up_parent(self):
		"""Recompute the parent box's derived columns.

		Frappe sets `parent`, `parenttype` and `parentfield` on a child row
		automatically, so the parent is known without this doctype having to
		carry a link of its own.

		`apply_rollup` writes with `db_set`, which does NOT re-enter the
		parent's document lifecycle — so this cannot loop back into itself.
		"""
		if not self.get("parent") or self.parenttype != "LaraTik Cash Box":
			return
		frappe.get_doc("LaraTik Cash Box", self.parent).apply_rollup()
