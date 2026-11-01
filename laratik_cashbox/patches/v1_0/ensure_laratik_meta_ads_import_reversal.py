"""Verify the Meta billing import vocabulary after schema sync.

─── WHY THIS PATCH EXISTS ─────────────────────────────────────────────────────

A reversal is a REVERSAL: it undoes a prior Meta charge. Before this change
the stored `record_kind` had no name for it, so a reversal was stored as a
Refund (when it was negative) or as Unclassified (when it was positive). The
first is a financial misstatement and the second hides a known fact inside the
"needs review" bucket.

`verify_doctype` checks that the site HAS the declared fields, but a Select
field's OPTIONS are not a field. So this patch checks the option list itself:
without it, a site that synced an older schema would accept "Reversal" rows
and store them as something else, or reject them outright, with no
migration-time complaint.
"""

import frappe

from laratik_cashbox import meta_ads_import as import_rules


REQUIRED_PAYMENT_FIELDS = ("reversal_count", "reversal_total")


def execute() -> None:
	"""Fail migration if the site's billing schema cannot hold a reversal."""
	problems = []

	payment_meta = frappe.get_meta("LaraTik Meta Ads Payment")
	record_kind = payment_meta.get_field("record_kind")
	if not record_kind:
		problems.append("LaraTik Meta Ads Payment.record_kind is missing")
	else:
		options = [option.strip() for option in (record_kind.options or "").split("\n") if option.strip()]
		for kind in import_rules.IMPORT_RECORD_KINDS:
			if kind not in options:
				problems.append(f"LaraTik Meta Ads Payment.record_kind cannot hold '{kind}'")

	import_meta = frappe.get_meta("LaraTik Meta Ads Payment Import")
	for fieldname in REQUIRED_PAYMENT_FIELDS:
		if not import_meta.has_field(fieldname):
			problems.append(f"LaraTik Meta Ads Payment Import.{fieldname} is missing")

	if problems:
		frappe.throw(
			"The Meta billing import must be able to store a reversal and its total. "
			"Run `bench --site <site> migrate` after syncing the current app schema. "
			+ "; ".join(problems)
		)
