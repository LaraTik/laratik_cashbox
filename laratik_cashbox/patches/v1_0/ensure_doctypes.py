"""Post-install verification that the live schema matches this repository.

─── WHY A PATCH THAT ONLY CHECKS ────────────────────────────────────────────

`bench migrate` already syncs a Frappe app's DocType JSON into the site's
DocType records — that is the mechanism by which a schema change reaches an
instance. So a patch that "applies" the schema would be a second mechanism
doing the same job, and the two would be able to disagree.

What a patch CAN do that the sync cannot is FAIL LOUDLY when the sync did not
land. A site can be restored from a backup taken before the app was
installed, or an app can be half-installed, and in both cases the DocType
records can be missing fields the application is about to read and write. The
application's own read-contract audit (ADR-097) can only notice a wrong
fieldname in a RESPONSE — long after the write that mattered. This patch
notices it at migrate time, with a message that names the field.

─── WHY THE EXPECTATION IS READ FROM THE JSON, NOT FROM A PYTHON LIST ───────

The JSON in this repository IS the expected schema. A second copy of the
field list, written in Python, would be a second source of truth that could
drift from the first — and the whole point of this app is that the schema and
the application cannot drift silently. So the patch reads the same JSON the
generator reads and the conformance test reads, and compares it with the live
meta.

─── WHAT THIS PATCH DOES NOT DO ─────────────────────────────────────────────

It does not create, alter or repair anything. It reads the site and the
repository, and it refuses to let a migrate finish on a schema that does not
match. Repairing is `bench --site <site> migrate` with the app installed,
and that is the whole repair procedure.
"""

import json
import os

import frappe

# The install order, dependency first.
#
# `LaraTik Cash Box Transaction` and `LaraTik Cash Box Transfer` come before `LaraTik Cash Box`
# because the parent's Table fields name them, and a Table field's target has
# to exist when the parent is imported.
#
# `LaraTik Cash Box Entry.other_box` links BACK to `LaraTik Cash Box`, so the pair is
# mutually referential and no order can satisfy both directions at once. It
# works because Frappe resolves a Link's target when a DOCUMENT is written,
# not when a doctype is imported — and a transfer entry is only ever written
# after both doctypes are installed.
DOCTYPES = [
	("LaraTik Cash Movement", "laratik_cash_movement"),
	("LaraTik Cash Box Entry", "laratik_cash_box_entry"),
	("LaraTik Cash Box Pending", "laratik_cash_box_pending"),
	("LaraTik Cash Box", "laratik_cash_box"),
	("LaraTik Meta Ads Page Asset", "laratik_meta_ads_page_asset"),
	("LaraTik Meta Ads Fee Rule", "laratik_meta_ads_fee_rule"),
	("LaraTik Meta Ads Connection", "laratik_meta_ads_connection"),
	("LaraTik Meta Ads Billing Profile", "laratik_meta_ads_billing_profile"),
	("LaraTik Meta Ads Account", "laratik_meta_ads_account"),
	("LaraTik Meta Ads Spend", "laratik_meta_ads_spend"),
	("LaraTik Meta Ads Sync", "laratik_meta_ads_sync"),
	("LaraTik Meta Ads Payment Import", "laratik_meta_ads_payment_import"),
	("LaraTik Meta Ads Payment", "laratik_meta_ads_payment"),
	("LaraTik Collection", "laratik_collection"),
	("LaraTik Item", "laratik_item"),
	("LaraTik Metric", "laratik_metric"),
	("LaraTik Artifact", "laratik_artifact"),
	("LaraTik Index Entry", "laratik_index_entry"),
	("LaraTik Setup State", "laratik_setup_state"),
]


def _repo_json_path(folder: str) -> str:
	"""The path of a doctype's JSON inside this app, on disk."""
	return os.path.join(
		frappe.get_module_path("laratik_cashbox"),
		"doctype",
		folder,
		f"{folder}.json",
	)


def _expected_fieldnames(folder: str) -> set:
	"""The fieldnames the repository's JSON declares for a doctype."""
	path = _repo_json_path(folder)
	if not os.path.exists(path):
		frappe.throw(
			f"laratik_cashbox is installed but {path} is missing. The app's files are incomplete; "
			"re-fetch the app and run `bench --site <site> migrate` again."
		)
	with open(path, encoding="utf-8") as handle:
		raw = json.load(handle)
	return {f["fieldname"] for f in raw.get("fields", []) if f.get("fieldname")}


def verify_doctype(doctype: str, folder: str) -> None:
	"""Fail the migrate when the live doctype does not match the repository."""
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(
			f"{doctype} is not installed on this site, but the LaraTik Cashbox application "
			"reads and writes it. Run `bench --site <site> migrate` with laratik_cashbox "
			"installed, and check the app's /settings/readiness screen afterwards."
		)

	expected = _expected_fieldnames(folder)
	meta = frappe.get_meta(doctype)
	missing = sorted(name for name in expected if not meta.has_field(name))
	if missing:
		frappe.throw(
			f"{doctype} on this site is missing the field(s) {', '.join(missing)} that "
			"laratik_cashbox declares. The application's writes and reads are not safe "
			"against this schema. Run `bench --site <site> migrate` with laratik_cashbox "
			"installed."
		)


def laratik_cash_box_entry() -> None:
	"""Verify `LaraTik Cash Box Entry`."""
	verify_doctype("LaraTik Cash Box Entry", "laratik_cash_box_entry")


def laratik_cash_movement() -> None:
	"""Verify the standalone movement schema synced."""
	verify_doctype("LaraTik Cash Movement", "laratik_cash_movement")


def cash_movement_workflow() -> None:
	"""Install the pre-submission approval and submission state machine once."""
	state_field = frappe.get_meta("LaraTik Cash Movement").get_field("workflow_state")
	if not state_field or state_field.fieldtype != "Link" or state_field.options != "Workflow State":
		frappe.throw(
			"LaraTik Cash Movement.workflow_state must be a Link to Workflow State, matching "
			"Frappe v16 Workflow. Run migrate after syncing the corrected DocType schema."
		)
	name = "LaraTik Cash Movement Approval"
	states = [
		{"state": "Draft", "doc_status": "0", "allow_edit": "Accounts User"},
		{"state": "Pending Approval", "doc_status": "0", "allow_edit": "Accounts Manager"},
		{"state": "Approved", "doc_status": "0", "allow_edit": "Accounts Manager"},
		{"state": "Rejected", "doc_status": "0", "allow_edit": "Accounts Manager"},
		{"state": "Submitted", "doc_status": "1", "allow_edit": "Accounts Manager"},
	]
	transitions = [
		{"state": "Draft", "action": "Request Approval", "next_state": "Pending Approval", "allowed": "Accounts User", "allow_self_approval": 1},
		{"state": "Pending Approval", "action": "Approve", "next_state": "Approved", "allowed": "Accounts Manager", "allow_self_approval": 0},
		{"state": "Pending Approval", "action": "Reject", "next_state": "Rejected", "allowed": "Accounts Manager", "allow_self_approval": 0},
		{"state": "Pending Approval", "action": "Return for Changes", "next_state": "Draft", "allowed": "Accounts Manager", "allow_self_approval": 1},
		{"state": "Approved", "action": "Return for Changes", "next_state": "Draft", "allowed": "Accounts Manager", "allow_self_approval": 1},
		{"state": "Rejected", "action": "Return for Changes", "next_state": "Draft", "allowed": "Accounts Manager", "allow_self_approval": 1},
		{"state": "Approved", "action": "Submit", "next_state": "Submitted", "allowed": "Accounts User", "allow_self_approval": 1},
	]
	if frappe.db.exists("Workflow", name):
		workflow = frappe.get_doc("Workflow", name)
		if workflow.document_type != "LaraTik Cash Movement" or not workflow.is_active:
			frappe.throw(f"{name} exists but is not the active workflow for LaraTik Cash Movement.")
		actual_states = [{"state": row.state, "doc_status": str(row.doc_status), "allow_edit": row.allow_edit} for row in workflow.states]
		actual_transitions = [{"state": row.state, "action": row.action, "next_state": row.next_state,
		                       "allowed": row.allowed, "allow_self_approval": int(row.allow_self_approval or 0)}
		                      for row in workflow.transitions]
		if actual_states != states or actual_transitions != transitions or workflow.workflow_state_field != "workflow_state":
			frappe.throw(f"{name} exists but does not match the required state machine. Review it before migration.")
		return
	for state in states:
		if not frappe.db.exists("Workflow State", state["state"]):
			frappe.get_doc({
				"doctype": "Workflow State",
				"workflow_state_name": state["state"],
			}).insert(ignore_permissions=True)
	for action in dict.fromkeys(row["action"] for row in transitions):
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc({
				"doctype": "Workflow Action Master",
				"workflow_action_name": action,
			}).insert(ignore_permissions=True)
	frappe.get_doc({
		"doctype": "Workflow",
		"workflow_name": name,
		"document_type": "LaraTik Cash Movement",
		"is_active": 1,
		"workflow_state_field": "workflow_state",
		"states": states,
		"transitions": transitions,
	}).insert(ignore_permissions=True)


def after_install() -> None:
	"""Create install-time setup skipped when Frappe marks patches complete."""
	cash_movement_workflow()
	laratik_meta_ads_page_asset()
	laratik_meta_ads_fee_rule()
	laratik_meta_ads_connection()
	laratik_meta_ads_billing_profile()
	laratik_meta_ads_account()
	laratik_meta_ads_spend()
	laratik_meta_ads_sync()
	laratik_meta_ads_payment_import()
	laratik_meta_ads_payment()


def laratik_cash_box_pending() -> None:
	"""Verify `LaraTik Cash Box Pending`."""
	verify_doctype("LaraTik Cash Box Pending", "laratik_cash_box_pending")


def laratik_cash_box() -> None:
	"""Verify `LaraTik Cash Box`."""
	verify_doctype("LaraTik Cash Box", "laratik_cash_box")


def laratik_collection() -> None:
	"""Verify `LaraTik Collection`."""
	verify_doctype("LaraTik Collection", "laratik_collection")


def laratik_item() -> None:
	"""Verify `LaraTik Item`."""
	verify_doctype("LaraTik Item", "laratik_item")


def laratik_metric() -> None:
	"""Verify `LaraTik Metric`."""
	verify_doctype("LaraTik Metric", "laratik_metric")


def laratik_artifact() -> None:
	"""Verify `LaraTik Artifact`."""
	verify_doctype("LaraTik Artifact", "laratik_artifact")


def laratik_index_entry() -> None:
	"""Verify `LaraTik Index Entry`."""
	verify_doctype("LaraTik Index Entry", "laratik_index_entry")


def laratik_setup_state() -> None:
	"""Verify the resumable setup state singleton synced."""
	verify_doctype("LaraTik Setup State", "laratik_setup_state")


def laratik_meta_ads_page_asset() -> None:
	"""Verify the Meta Page/Instagram asset child schema synced."""
	verify_doctype("LaraTik Meta Ads Page Asset", "laratik_meta_ads_page_asset")


def laratik_meta_ads_fee_rule() -> None:
	"""Verify the dated Meta billing-profile fee rule schema synced."""
	verify_doctype("LaraTik Meta Ads Fee Rule", "laratik_meta_ads_fee_rule")


def laratik_meta_ads_connection() -> None:
	"""Verify the encrypted Meta connection schema synced."""
	verify_doctype("LaraTik Meta Ads Connection", "laratik_meta_ads_connection")


def laratik_meta_ads_billing_profile() -> None:
	"""Verify the customer/third-party billing profile schema synced."""
	verify_doctype("LaraTik Meta Ads Billing Profile", "laratik_meta_ads_billing_profile")


def laratik_meta_ads_account() -> None:
	"""Verify the Meta ad-account schema synced."""
	verify_doctype("LaraTik Meta Ads Account", "laratik_meta_ads_account")


def laratik_meta_ads_spend() -> None:
	"""Verify daily Meta Insights spend snapshots synced."""
	verify_doctype("LaraTik Meta Ads Spend", "laratik_meta_ads_spend")


def laratik_meta_ads_sync() -> None:
	"""Verify Meta spend sync audit records synced."""
	verify_doctype("LaraTik Meta Ads Sync", "laratik_meta_ads_sync")


def laratik_meta_ads_payment_import() -> None:
	"""Verify the Meta billing-report import batch schema synced."""
	verify_doctype("LaraTik Meta Ads Payment Import", "laratik_meta_ads_payment_import")


def laratik_meta_ads_payment() -> None:
	"""Verify immutable Meta payment records synced."""
	verify_doctype("LaraTik Meta Ads Payment", "laratik_meta_ads_payment")
