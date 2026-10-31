"""Verify account-level Meta payment snapshots are optional after schema sync."""

import frappe


OPTIONAL_SNAPSHOT_FIELDS = {
	"LaraTik Meta Ads Payment": (
		"billing_profile_snapshot",
		"billing_customer_snapshot",
		"relationship_kind_snapshot",
		"fee_percent_snapshot",
		"invoice_currency_snapshot",
	),
	"LaraTik Meta Ads Payment Import": (
		"billing_profile_snapshot",
		"billing_customer_snapshot",
		"relationship_kind_snapshot",
		"fee_percent_snapshot",
		"invoice_currency_snapshot",
	),
}


def execute() -> None:
	"""Fail migration if either payment DocType still requires a payer profile."""
	problems = []
	for doctype, fieldnames in OPTIONAL_SNAPSHOT_FIELDS.items():
		meta = frappe.get_meta(doctype)
		for fieldname in fieldnames:
			field = meta.get_field(fieldname)
			if not field:
				problems.append(f"{doctype}.{fieldname} is missing")
			elif field.reqd:
				problems.append(f"{doctype}.{fieldname} is still required")

	if problems:
		frappe.throw(
			"Meta payment records must allow unassigned account-level evidence. "
			"Run `bench --site <site> migrate` after syncing the current app schema. "
			+ "; ".join(problems)
		)
