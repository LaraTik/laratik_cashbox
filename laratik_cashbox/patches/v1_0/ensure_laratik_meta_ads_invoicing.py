"""Verify the Meta invoice schema after sync.

─── WHY THIS PATCH EXISTS ─────────────────────────────────────────────────────

Stage 4 adds the two things an invoice needs and the ledger did not have:

1. **A pinned invoice state** — the `LaraTik Meta Ads Invoice Batch` and its
   chargeable `LaraTik Meta Ads Invoice Line` rows. Without them there is
   nothing that says a period was invoiced, so a re-sync can rewrite a period
   nobody could tell had been billed (F-08), and there is nothing that says
   which ERPNext Sales Invoice a charge became.

2. **The policy answers, as states** — the billing profile's charge model,
   recognition model, income account, invoice item and tax treatment, and the
   flag that says the service fee was actually stated.

The second one is the fix for F-03. `fee_percent` has a default of `0`, so a
profile nobody ever touched reads as a zero fee, and a zero fee is a
legitimate answer. The flag tells them apart: a fee that was never stated is
not a zero fee, and an invoice that depends on it waits for somebody to say
what the fee is.

The schema reaches the site through the DocType sync, so this patch runs
AFTER it and checks reality: every field the repository declares must exist
on the site, or the application would read a field that is not there and
quietly treat it as absent.
"""

import frappe

from laratik_cashbox import meta_ads_invoicing as invoicing_rules
from laratik_cashbox.patches.v1_0.ensure_doctypes import (
	laratik_meta_ads_invoice_batch,
	laratik_meta_ads_invoice_line,
)


REQUIRED_PROFILE_FIELDS = (
	"fee_is_configured",
	"charge_model",
	"recognition_model",
	"income_account",
	"invoice_item",
	"tax_treatment",
	"tax_template",
)

REQUIRED_SPEND_FIELDS = ("fee_status_snapshot",)

#: The Select fields whose OPTIONS carry the policy vocabulary. A Select's
#: options are not fields, so `has_field` cannot see them: a site that synced
#: an older schema would accept "Recharge" and store it as something else, or
#: drop it — with no migration-time complaint. Each entry is
#: (fieldname, the vocabulary it must be able to hold).
REQUIRED_SELECT_VOCABULARY = (
	("charge_model", invoicing_rules.CHARGE_MODELS),
	("recognition_model", invoicing_rules.RECOGNITION_MODELS),
	("tax_treatment", invoicing_rules.TAX_TREATMENTS),
)


def execute() -> None:
	"""Fail migration if the site's invoice schema cannot hold an invoice."""
	laratik_meta_ads_invoice_batch()
	laratik_meta_ads_invoice_line()

	problems = []
	profile_meta = frappe.get_meta("LaraTik Meta Ads Billing Profile")
	for fieldname in REQUIRED_PROFILE_FIELDS:
		if not profile_meta.has_field(fieldname):
			problems.append(f"LaraTik Meta Ads Billing Profile.{fieldname} is missing")

	for fieldname, vocabulary in REQUIRED_SELECT_VOCABULARY:
		field = profile_meta.get_field(fieldname)
		if not field:
			# Already reported as a missing field above.
			continue
		options = [option.strip() for option in (field.options or "").split("\n") if option.strip()]
		for value in vocabulary:
			if value not in options:
				problems.append(
					f"LaraTik Meta Ads Billing Profile.{fieldname} cannot hold '{value}'"
				)

	spend_meta = frappe.get_meta("LaraTik Meta Ads Spend")
	for fieldname in REQUIRED_SPEND_FIELDS:
		if not spend_meta.has_field(fieldname):
			problems.append(f"LaraTik Meta Ads Spend.{fieldname} is missing")

	if problems:
		frappe.throw(
			"The Meta invoice workspace needs the billing profile's policy fields and the "
			"spend snapshot's fee status. Run `bench --site <site> migrate` after syncing "
			"the current app schema. " + "; ".join(problems)
		)
