"""Import Meta billing exports as company-scoped payment evidence.

Meta's current published Marketing API contract does not document this payment
history. This module accepts the account's Meta invoice-summary CSV and records
the source rows in a separate, non-posting ledger. It never creates an ERPNext
invoice or GL entry.
"""

import base64
import binascii
import csv
import hashlib
import io
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

import frappe
from frappe.utils import now_datetime
from frappe.utils.file_manager import save_file

from laratik_cashbox import meta_ads_import as import_rules
from laratik_cashbox.meta_ads_api import (
	_company_name,
	_currency_code,
	_date_range,
	_decimal,
	_require_admin,
	_text,
	_fee_percent_for_date,
)


MAX_CSV_BYTES = 4 * 1024 * 1024
MAX_IMPORT_ROWS = 50_000
MAX_REPORT_ROWS = 20_000
PAYMENT_PAGE_SIZE = 100
MAX_ACCOUNT_COUNT = 500
MAX_IMPORT_HISTORY_ROWS = 100
MAX_PREVIEW_ROWS = 200
MAX_PROBLEM_ROWS = 100
META_BILLING_SOURCE = "Meta invoice summary CSV"
CSV_AMOUNT_PATTERN = re.compile(r"^-?\d+(?:\.\d+)?$")
CSV_GROUPED_AMOUNT_PATTERN = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
META_ACCOUNT_PATTERN = re.compile(r"^(?:act_)?(\d+)$", re.IGNORECASE)
META_REPORT_PERIOD_PATTERN = re.compile(
	r"^\s*(.+?)\s+(?:-|–|—|to)\s+(.+?)\s*$",
	re.IGNORECASE,
)


@frappe.whitelist(methods=["POST"])
def preview_billing_report(company, account_name, file_name, csv_base64):
	"""Validate a Meta billing CSV without saving its contents or creating rows."""
	_require_admin()
	company_name = _company_name(company)
	prepared = _prepare_import(company_name, account_name, file_name, csv_base64)
	return prepared["preview"]


@frappe.whitelist(methods=["POST"])
def import_billing_report(company, account_name, file_name, csv_base64):
	"""Store an imported Meta billing report and its immutable payment rows."""
	_require_admin()
	company_name = _company_name(company)
	prepared = _prepare_import(company_name, account_name, file_name, csv_base64)
	preview = prepared["preview"]
	if preview["problem_count"]:
		frappe.throw(
			"The Meta billing CSV holds "
			f"{preview['problem_count']} problem{'s' if preview['problem_count'] != 1 else ''} "
			f"({_problems_summary(preview['problems'])}). "
			f"{_problem_sentences(preview['problems'])} "
			"The file was not imported: a billing cycle that does not reconcile with its own "
			"totals is not partly stored."
		)
	if preview["conflict_count"]:
		frappe.throw("The report has conflicting transaction IDs. Resolve them before importing.")

	account = prepared["account"]
	profile = prepared["profile"]
	parsed = prepared["parsed"]
	new_rows = prepared["new_rows"]
	batch_snapshot = _profile_snapshot(profile, parsed["end_date"])

	imported_at = now_datetime()
	batch = frappe.get_doc({
		"doctype": "LaraTik Meta Ads Payment Import",
		"company": company_name,
		"ad_account": account.name,
		"external_account_id": account.external_account_id,
		"billing_profile_snapshot": batch_snapshot["profile"],
		"billing_customer_snapshot": batch_snapshot["customer"],
		"relationship_kind_snapshot": batch_snapshot["relationship_kind"],
		"fee_percent_snapshot": batch_snapshot["fee_percent"],
		"invoice_currency_snapshot": batch_snapshot["invoice_currency"],
		"page_assets_snapshot": batch_snapshot["page_assets"],
		"report_start_date": parsed["start_date"],
		"report_end_date": parsed["end_date"],
		"currency": parsed["currency"],
		"source_sha256": parsed["sha256"],
		"source": META_BILLING_SOURCE,
		"status": "Imported",
		"report_row_count": parsed["report_row_count"],
		"inserted_row_count": len(new_rows),
		"duplicate_row_count": preview["duplicate_count"],
		"payment_count": parsed["counts"]["Payment"],
		"refund_count": parsed["counts"]["Refund"],
		"credit_count": parsed["counts"]["Credit"],
		"reversal_count": parsed["counts"]["Reversal"],
		"unclassified_count": parsed["counts"]["Unclassified"],
		"payment_total": parsed["totals"]["Payment"],
		"refund_total": parsed["totals"]["Refund"],
		"credit_total": parsed["totals"]["Credit"],
		"reversal_total": parsed["totals"]["Reversal"],
		"unclassified_total": parsed["totals"]["Unclassified"],
		"imported_at": imported_at,
		"imported_by": frappe.session.user,
	})
	batch.insert(ignore_permissions=True)

	file_doc = save_file(
		prepared["file_name"],
		prepared["file_bytes"],
		"LaraTik Meta Ads Payment Import",
		batch.name,
		is_private=1,
	)
	batch.db_set("source_file", file_doc.name, update_modified=False)

	for row in new_rows:
		row_snapshot = _profile_snapshot(profile, row["transaction_date"])
		payment = frappe.get_doc({
			"doctype": "LaraTik Meta Ads Payment",
			"transaction_key": row["transaction_key"],
			"import_batch": batch.name,
			"company": company_name,
			"ad_account": account.name,
			"external_account_id": account.external_account_id,
			"billing_profile_snapshot": row_snapshot["profile"],
			"billing_customer_snapshot": row_snapshot["customer"],
			"relationship_kind_snapshot": row_snapshot["relationship_kind"],
			"fee_percent_snapshot": row_snapshot["fee_percent"],
			"invoice_currency_snapshot": row_snapshot["invoice_currency"],
			"page_assets_snapshot": row_snapshot["page_assets"],
			"transaction_date": row["transaction_date"],
			"transaction_id": row["transaction_id"],
			"amount": row["amount"],
			"currency": row["currency"],
			"payment_method": row["payment_method"],
			"record_kind": row["record_kind"],
			"source": META_BILLING_SOURCE,
			"source_file": file_doc.name,
			"imported_at": imported_at,
		})
		payment.insert(ignore_permissions=True)

	return {
		"batch": batch.name,
		"report_row_count": parsed["report_row_count"],
		"inserted_row_count": len(new_rows),
		"duplicate_row_count": preview["duplicate_count"],
		"start_date": parsed["start_date"],
		"end_date": parsed["end_date"],
		"currency": parsed["currency"],
		"assignment_status": "Mapped" if profile else "Unassigned",
		"payment_total": parsed["totals"]["Payment"],
		"refund_total": parsed["totals"]["Refund"],
		"credit_total": parsed["totals"]["Credit"],
		"reversal_total": parsed["totals"]["Reversal"],
		"unclassified_total": parsed["totals"]["Unclassified"],
		"source_sha256": parsed["sha256"],
		"source_file": file_doc.name,
		"section_totals_verified": parsed["section_totals_verified"],
	}


@frappe.whitelist(methods=["GET"])
def get_billing_report(company, since, until, account_name=None, page=0):
	"""Read imported transactions and monthly payer totals for one company."""
	_require_admin()
	company_name = _company_name(company)
	start, end = _date_range(since, until)
	try:
		page_number = int(page)
	except (TypeError, ValueError):
		frappe.throw("The payment page must be a non-negative whole number.")
	if page_number < 0 or page_number > 10_000:
		frappe.throw("The payment page is outside the supported range.")
	accounts = frappe.get_all(
		"LaraTik Meta Ads Account",
		filters={"company": company_name},
		fields=["name", "external_account_id", "account_name", "currency", "billing_profile"],
		order_by="account_name asc",
		limit_page_length=MAX_ACCOUNT_COUNT + 1,
	)
	accounts_truncated = len(accounts) > MAX_ACCOUNT_COUNT
	accounts = accounts[:MAX_ACCOUNT_COUNT]
	account_by_name = {row.name: row for row in accounts}
	if account_name and account_name not in account_by_name:
		frappe.throw("Choose a Meta ad account belonging to this Company.")

	filters = {
		"company": company_name,
		"transaction_date": ["between", [start.isoformat(), end.isoformat()]],
	}
	if account_name:
		filters["ad_account"] = account_name
	payments = frappe.get_all(
		"LaraTik Meta Ads Payment",
		filters=filters,
		fields=[
			"name", "import_batch", "ad_account", "external_account_id", "billing_profile_snapshot",
			"billing_customer_snapshot", "relationship_kind_snapshot", "fee_percent_snapshot",
			"invoice_currency_snapshot", "page_assets_snapshot", "transaction_date", "transaction_id",
			"amount", "currency", "payment_method", "record_kind", "source", "imported_at",
		],
		order_by="transaction_date desc, ad_account asc, transaction_id asc",
		limit_page_length=MAX_REPORT_ROWS + 1,
	)
	rows_truncated = len(payments) > MAX_REPORT_ROWS
	payments = payments[:MAX_REPORT_ROWS]

	profile_cache = {}
	group_map = {}
	rows = []
	unreadable_count = 0
	# TRACK C — the settlement evidence, counted across every record.
	#
	# It is kept SEPARATE from the delivery spend and it is never compared
	# with it. Meta's billing and payment records cover billing CYCLES, so
	# they do not line up one-to-one with an arbitrary range of delivery
	# dates, and a difference between the two is not an accounting error — it
	# is two different questions about two different things.
	settlement_totals = {
		"payments": defaultdict(Decimal),
		"refunds": defaultdict(Decimal),
		"credits": defaultdict(Decimal),
		"reversals": defaultdict(Decimal),
		"unclassified": defaultdict(Decimal),
	}
	settlement_unclassified_count = 0
	for raw in payments:
		account = account_by_name.get(raw.ad_account)
		if not account or raw.external_account_id != account.external_account_id:
			unreadable_count += 1
			continue
		try:
			amount = _source_amount(raw.amount)
			currency = _currency_code(raw.currency)
			if currency != account.currency or raw.record_kind not in import_rules.IMPORT_RECORD_KINDS:
				raise ValueError("Stored transaction does not match its account.")
			fee_percent = _decimal(raw.fee_percent_snapshot or "0")
			if fee_percent < 0 or fee_percent > 100:
				raise ValueError("Stored service percentage is invalid.")
			page_assets = json.loads(raw.page_assets_snapshot or "[]")
			if not isinstance(page_assets, list):
				raise ValueError("Stored Page snapshot is invalid.")
		except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
			unreadable_count += 1
			continue

		profile_name = raw.billing_profile_snapshot or ""
		if profile_name not in profile_cache:
			profile_cache[profile_name] = _profile_names(raw.billing_customer_snapshot)
		profile = profile_cache[profile_name]
		assignment_status = "Mapped" if profile_name else "Unassigned"
		customer_name = profile["customer_name"] if profile_name else _unassigned_account_label(account)
		group_key = "|".join((
			profile_name or f"unassigned:{account.name}",
			_decimal_text(fee_percent) if profile_name else "unassigned",
			raw.invoice_currency_snapshot or "",
			(raw.relationship_kind_snapshot or "Customer") if profile_name else "",
		))
		if group_key not in group_map:
			group_map[group_key] = {
				"key": group_key,
				"assignment_status": assignment_status,
				"profile": profile_name,
				"customer": raw.billing_customer_snapshot or "",
				"customer_name": customer_name,
				"relationship_kind": (raw.relationship_kind_snapshot or "Customer") if profile_name else "",
				"fee_percent": _decimal_text(fee_percent) if profile_name else "",
				"invoice_currency": raw.invoice_currency_snapshot or "",
				"page_assets": [],
				"accounts": {},
				"payment": defaultdict(Decimal),
				"refund": defaultdict(Decimal),
				"credit": defaultdict(Decimal),
				"reversal": defaultdict(Decimal),
				"unclassified": defaultdict(Decimal),
				"unclassified_count": 0,
				"refund_count": 0,
			}
		group = group_map[group_key]
		group["accounts"][account.name] = account.account_name
		for asset in page_assets:
			if asset not in group["page_assets"]:
				group["page_assets"].append(asset)
		if raw.record_kind == "Payment":
			group["payment"][currency] += amount
			settlement_totals["payments"][currency] += amount
		elif raw.record_kind == "Refund":
			group["refund"][currency] += abs(amount)
			group["refund_count"] += 1
			settlement_totals["refunds"][currency] += abs(amount)
		elif raw.record_kind == "Credit":
			group["credit"][currency] += abs(amount)
			settlement_totals["credits"][currency] += abs(amount)
		elif raw.record_kind == "Reversal":
			# A reversal is money coming back, and it is NOT a refund: it
			# undoes a charge rather than returning one. It reduces the
			# settlement position, and it is named apart from a refund so the
			# reader is not told LaraTik refunded money when Meta reversed a
			# charge.
			group["reversal"][currency] += abs(amount)
			settlement_totals["reversals"][currency] += abs(amount)
		else:
			group["unclassified"][currency] += amount
			group["unclassified_count"] += 1
			settlement_totals["unclassified"][currency] += amount
			settlement_unclassified_count += 1

		rows.append({
			"name": raw.name,
			"batch": raw.import_batch,
			"account": account.name,
			"account_name": account.account_name,
			"external_account_id": raw.external_account_id,
			"profile": profile_name,
			"assignment_status": assignment_status,
			"customer": raw.billing_customer_snapshot or "",
			"customer_name": customer_name,
			"transaction_date": str(raw.transaction_date),
			"transaction_id": raw.transaction_id,
			"amount": format(amount, "f"),
			"currency": currency,
			"payment_method": raw.payment_method or "",
			"record_kind": raw.record_kind,
			"source": raw.source or META_BILLING_SOURCE,
			"imported_at": str(raw.imported_at or ""),
		})

	complete = not (accounts_truncated or rows_truncated or unreadable_count)
	billing_groups = []
	for group in group_map.values():
		payment_amounts = group["payment"]
		refund_amounts = group["refund"]
		credit_amounts = group["credit"]
		reversal_amounts = group["reversal"]
		unclassified_amounts = group["unclassified"]
		billing_groups.append({
			"key": group["key"],
			"assignment_status": group["assignment_status"],
			"profile": group["profile"],
			"customer": group["customer"],
			"customer_name": group["customer_name"],
			"relationship_kind": group["relationship_kind"],
			"fee_percent": group["fee_percent"],
			"invoice_currency": group["invoice_currency"],
			"page_assets": group["page_assets"],
			"accounts": [{"name": key, "account_name": value} for key, value in sorted(group["accounts"].items())],
			"payments": _decimal_map(payment_amounts),
			"refunds": _decimal_map(refund_amounts),
			"credits": _decimal_map(credit_amounts),
			"reversals": _decimal_map(reversal_amounts),
			"unclassified": _decimal_map(unclassified_amounts),
			"unclassified_count": group["unclassified_count"],
			"refund_count": group["refund_count"],
			"requires_review": bool(group["unclassified_count"]),
		})

	imports = frappe.get_all(
		"LaraTik Meta Ads Payment Import",
		filters={
			"company": company_name,
			"report_start_date": ["<=", end.isoformat()],
			"report_end_date": [">=", start.isoformat()],
			**({"ad_account": account_name} if account_name else {}),
		},
		fields=[
			"name", "ad_account", "external_account_id", "billing_profile_snapshot", "billing_customer_snapshot", "report_start_date",
			"report_end_date", "currency", "report_row_count", "inserted_row_count", "duplicate_row_count",
			"source", "source_file", "imported_at",
		],
		order_by="imported_at desc",
		limit_page_length=MAX_IMPORT_HISTORY_ROWS + 1,
	)
	imports_truncated = len(imports) > MAX_IMPORT_HISTORY_ROWS
	imports = imports[:MAX_IMPORT_HISTORY_ROWS]
	customer_names = {}
	for customer in {row.billing_customer_snapshot for row in imports if row.billing_customer_snapshot}:
		customer_names[customer] = frappe.db.get_value("Customer", customer, "customer_name") or customer

	# TRACK C — the settlement position, per currency.
	#
	# A refund and a credit are money coming BACK, so they REDUCE the position.
	# An unclassified record keeps its own sign: it is not a known direction,
	# and pretending it is one would be inventing a settlement.
	settlement_currencies = sorted(
		{currency for bucket in settlement_totals.values() for currency in bucket}
	)
	settlement_net = {}
	for currency in settlement_currencies:
		settlement_net[currency] = import_rules.settlement_net(
			settlement_totals["payments"].get(currency, Decimal(0)),
			settlement_totals["refunds"].get(currency, Decimal(0)),
			settlement_totals["credits"].get(currency, Decimal(0)),
			settlement_totals["reversals"].get(currency, Decimal(0)),
			settlement_totals["unclassified"].get(currency, Decimal(0)),
		)
	settlement = {
		"status": "Evidence available" if settlement_currencies else "No evidence",
		"record_count": len(rows),
		"unclassified_count": settlement_unclassified_count,
		"requires_review": bool(settlement_unclassified_count),
		"payments": _decimal_map(settlement_totals["payments"]),
		"refunds": _decimal_map(settlement_totals["refunds"]),
		"credits": _decimal_map(settlement_totals["credits"]),
		"reversals": _decimal_map(settlement_totals["reversals"]),
		"unclassified": _decimal_map(settlement_totals["unclassified"]),
		"net": {currency: format(amount, "f") for currency, amount in settlement_net.items()},
		"currencies": settlement_currencies,
		"note": (
			"Settlement is what Meta billed and LaraTik paid or received back. "
			"It is not delivery spend, and the two are not expected to match for "
			"an arbitrary range of dates: Meta bills in cycles, and a cycle "
			"covers dates outside the range being reported."
		),
	}

	return {
		"company": company_name,
		"since": start.isoformat(),
		"until": end.isoformat(),
		"complete": complete,
		"truncated": accounts_truncated or rows_truncated,
		"unreadable_count": unreadable_count,
		# TRACK C — the settlement evidence, kept apart from delivery spend.
		"settlement": settlement,
		"accounts": [{
			"name": row.name,
			"external_account_id": row.external_account_id,
			"account_name": row.account_name,
			"currency": row.currency,
			"billing_profile": row.billing_profile or "",
		} for row in accounts],
		"rows": rows[page_number * PAYMENT_PAGE_SIZE:(page_number + 1) * PAYMENT_PAGE_SIZE],
		"row_count": len(rows) if not rows_truncated else MAX_REPORT_ROWS + 1,
		"page": page_number,
		"page_size": PAYMENT_PAGE_SIZE,
		"page_count": max(1, (min(len(rows), MAX_REPORT_ROWS) + PAYMENT_PAGE_SIZE - 1) // PAYMENT_PAGE_SIZE),
		"billing_groups": sorted(billing_groups, key=lambda row: row["customer_name"].casefold()),
		"imports_truncated": imports_truncated,
		"imports": [{
			"name": row.name,
			"account": row.ad_account,
			"account_name": account_by_name.get(row.ad_account).account_name if account_by_name.get(row.ad_account) else row.external_account_id,
			"external_account_id": row.external_account_id,
			"customer": row.billing_customer_snapshot or "",
			"customer_name": _import_customer_name(row, customer_names, account_by_name),
			"assignment_status": "Mapped" if row.billing_profile_snapshot else "Unassigned",
			"start_date": str(row.report_start_date),
			"end_date": str(row.report_end_date),
			"currency": row.currency,
			"report_row_count": row.report_row_count,
			"inserted_row_count": row.inserted_row_count,
			"duplicate_row_count": row.duplicate_row_count,
			"source": row.source,
			"source_file": row.source_file or "",
			"imported_at": str(row.imported_at or ""),
		} for row in imports],
		"source_label": META_BILLING_SOURCE,
		"payment_status_available": False,
	}


def _prepare_import(company_name, account_name, file_name, csv_base64):
	"""Read a billing CSV and describe exactly what an import would do with it.

	The preview is the guided flow's contract: it answers "what did you read",
	"what is wrong", "what is already stored" and "what would happen", before
	anything is written.
	"""
	account = frappe.get_doc("LaraTik Meta Ads Account", _text(account_name, "Meta ad account", 140))
	account.check_permission("read")
	if account.company != company_name:
		frappe.throw("The Meta ad account belongs to another Company.")
	profile = None
	if account.billing_profile:
		profile = frappe.get_doc("LaraTik Meta Ads Billing Profile", account.billing_profile)
		profile.check_permission("read")
		if profile.company != company_name or profile.disabled:
			frappe.throw("Choose an enabled billing profile for this Company before importing payments.")
		if not profile.customer or not frappe.db.exists("Customer", profile.customer):
			frappe.throw("The account's billing profile must name an existing Customer.")
		if not profile.invoice_currency or not frappe.db.exists("Currency", profile.invoice_currency):
			frappe.throw("The account's billing profile must name an enabled invoice currency.")
		if not frappe.db.get_value("Currency", profile.invoice_currency, "enabled"):
			frappe.throw("The account's billing profile must name an enabled invoice currency.")

	file_bytes, safe_name = _decode_upload(file_name, csv_base64)
	parsed = _parse_report(file_bytes, account)
	new_rows = []
	duplicate_rows = []
	duplicate_count = parsed["duplicate_in_file_count"]
	conflicts = list(parsed["conflicts"])
	for row in parsed["rows"]:
		existing = frappe.db.get_value(
			"LaraTik Meta Ads Payment",
			{"transaction_key": row["transaction_key"]},
			["name", "transaction_date", "amount", "currency", "payment_method", "record_kind"],
			as_dict=True,
		)
		if not existing:
			row["status"] = import_rules.IMPORT_ROW_NEW
			new_rows.append(row)
			continue
		if _existing_matches(existing, row):
			row["status"] = import_rules.IMPORT_ROW_DUPLICATE
			duplicate_count += 1
			duplicate_rows.append(row)
		else:
			row["status"] = import_rules.IMPORT_ROW_CONFLICT
			conflicts.append({"row_number": row["row_number"], "code": "TRANSACTION_DIFFERS"})

	problems = list(parsed.get("problems", []))
	counts = parsed["counts"]
	totals = parsed["totals"]
	previous = _previous_import(account.name, parsed["sha256"])
	can_import = not problems and not conflicts
	preview_rows = [
		{
			"row_number": row["row_number"],
			"transaction_date": row["transaction_date"],
			"transaction_id": row["transaction_id"],
			"amount": row["amount"],
			"currency": row["currency"],
			"payment_method": row["payment_method"],
			"record_kind": row["record_kind"],
			"status": row.get("status", import_rules.IMPORT_ROW_NEW),
		}
		for row in parsed["rows"]
	]
	return {
		"account": account,
		"profile": profile,
		"file_bytes": file_bytes,
		"file_name": safe_name,
		"parsed": parsed,
		"new_rows": new_rows,
		"preview": {
			"account": account.name,
			"account_name": account.account_name,
			"external_account_id": account.external_account_id,
			"customer": profile.customer if profile else "",
			"customer_name": (frappe.db.get_value("Customer", profile.customer, "customer_name") or profile.customer) if profile else _unassigned_account_label(account),
			"billing_profile": profile.name if profile else "",
			"assignment_status": "Mapped" if profile else "Unassigned",
			"relationship_kind": (profile.relationship_kind or "Customer") if profile else "",
			"fee_percent": _fee_percent_for_date(profile.as_dict(), parsed["end_date"]) if profile and parsed["end_date"] else "",
			"invoice_currency": profile.invoice_currency if profile else "",
			"start_date": parsed["start_date"],
			"end_date": parsed["end_date"],
			"currency": parsed["currency"],
			"row_count": parsed["report_row_count"],
			"payment_count": counts["Payment"],
			"refund_count": counts["Refund"],
			"credit_count": counts["Credit"],
			"reversal_count": counts["Reversal"],
			"unclassified_count": counts["Unclassified"],
			"payment_total": totals["Payment"],
			"refund_total": totals["Refund"],
			"credit_total": totals["Credit"],
			"reversal_total": totals["Reversal"],
			"unclassified_total": totals["Unclassified"],
			"duplicate_count": duplicate_count,
			"new_row_count": len(new_rows),
			"conflict_count": len(conflicts),
			"conflicts": conflicts[:MAX_PROBLEM_ROWS],
			"methods": sorted({row["payment_method"] for row in parsed["rows"] if row["payment_method"]}),
			"can_import": can_import,
			"payment_status_available": False,
			# The findings — every problem the file holds, not only the first.
			"problems": problems[:MAX_PROBLEM_ROWS],
			"problem_count": len(problems),
			"invalid_row_count": sum(1 for problem in problems if problem.get("scope") == "row"),
			# The parsed rows, so the operator can read what was read.
			"rows": preview_rows[:MAX_PREVIEW_ROWS],
			"row_preview_count": len(preview_rows),
			"row_preview_truncated": len(preview_rows) > MAX_PREVIEW_ROWS,
			# The file's own totals, and whether they were verified.
			"section_totals_verified": parsed["section_totals_verified"],
			"sha256": parsed["sha256"],
			# This exact file, imported before.
			"already_imported": previous,
		},
	}


def _previous_import(account_name, sha256):
	"""The import batch that already holds this exact file, if there is one.

	Idempotency has two levels. The row level is the transaction key: a row
	already stored is a DUPLICATE, never a second row. The file level is the
	hash: the same bytes are the same evidence, and saying so is more useful
	than letting the operator discover it from a row count.
	"""
	if not sha256:
		return None
	previous = frappe.db.get_value(
		"LaraTik Meta Ads Payment Import",
		{"ad_account": account_name, "source_sha256": sha256},
		["name", "imported_at", "inserted_row_count", "report_row_count"],
		as_dict=True,
	)
	if not previous:
		return None
	return {
		"name": previous.name,
		"imported_at": str(previous.imported_at or ""),
		"inserted_row_count": previous.inserted_row_count,
		"report_row_count": previous.report_row_count,
	}


def _decode_upload(file_name, csv_base64):
	if not isinstance(csv_base64, str) or not csv_base64 or len(csv_base64) > ((MAX_CSV_BYTES + 2) // 3) * 4 + 8:
		frappe.throw("Upload one CSV file no larger than 4 MB.")
	try:
		content = base64.b64decode(csv_base64, validate=True)
	except (binascii.Error, ValueError, TypeError):
		frappe.throw("The uploaded CSV data is invalid.")
	if not content or len(content) > MAX_CSV_BYTES:
		frappe.throw("Upload one CSV file no larger than 4 MB.")
	name = os.path.basename(_text(file_name, "CSV filename", 240)).strip()
	if not name.lower().endswith(".csv"):
		frappe.throw("Choose a Meta billing CSV file with a .csv extension.")
	return content, name[:140]


def _read_transaction(cells, row_number, date_order, report_period, account, method, method_is_known):
	"""Read ONE transaction row.

	Returns (row, problems, reconcilable).

	`reconcilable` says whether this row's amount, currency and method could be
	read. A row whose DATE is unreadable is still reconcilable — the file's own
	section total includes it — so the section check stays meaningful. A row
	whose AMOUNT is unreadable is not, and the section total cannot be
	verified against it.
	"""
	problems = []
	reconcilable = True

	transaction_date = None
	try:
		transaction_date = _parse_date(cells[0], date_order)
	except (ValueError, InvalidOperation, TypeError):
		# The DATE is what is unreadable, not the amount. The row is refused,
		# but its amount still counts towards the file's own section total,
		# because the file counted it.
		problems.append(_problem("row", "INVALID_DATE", "The transaction date could not be read.", row_number))
	if transaction_date is not None and report_period and not report_period[0] <= transaction_date <= report_period[1]:
		problems.append(_problem("row", "DATE_OUTSIDE_PERIOD", "The transaction date is outside the billing-report period.", row_number))
		transaction_date = None

	transaction_id = ""
	try:
		transaction_id = _text(cells[1], "Transaction ID", 140)
	except (ValueError, InvalidOperation, TypeError, frappe.ValidationError):
		transaction_id = ""
	if not transaction_id or transaction_id.casefold() == "nan":
		problems.append(_problem("row", "MISSING_TRANSACTION_ID", "The transaction ID is empty.", row_number))
		transaction_id = ""

	amount = None
	try:
		amount = _csv_amount(cells[2])
	except (ValueError, InvalidOperation, TypeError):
		problems.append(_problem("row", "INVALID_AMOUNT", "The amount is not a plain decimal.", row_number))
		reconcilable = False
	if amount is not None and amount == 0:
		problems.append(_problem("row", "ZERO_AMOUNT", "The transaction amount is zero.", row_number))
		amount = None
		reconcilable = False

	currency = ""
	try:
		currency = _currency_code(cells[3])
	except (ValueError, InvalidOperation, TypeError, frappe.ValidationError):
		problems.append(_problem("row", "INVALID_CURRENCY", "The currency could not be read.", row_number))
		reconcilable = False
	if currency and currency != account.currency:
		# The currency was READ, so the section can still be reconciled against
		# this row. The row itself is refused: it is not this account's money.
		problems.append(_problem("row", "CURRENCY_MISMATCH", "The transaction currency does not match the selected Meta ad account.", row_number))

	if not method_is_known:
		problems.append(_problem("row", "MISSING_PAYMENT_METHOD", "The transaction has no payment method.", row_number))
		reconcilable = False

	transaction_key = ""
	if transaction_id:
		transaction_key = hashlib.sha256(
			f"{_account_digits(account.external_account_id)}:{transaction_id}".encode("utf-8")
		).hexdigest()

	record_kind = import_rules.classify_record_kind(method, amount)
	row = {
		"row_number": row_number,
		"transaction_key": transaction_key,
		"transaction_date": transaction_date.isoformat() if transaction_date else "",
		"transaction_id": transaction_id,
		"amount": _canonical_amount(amount) if amount is not None else "",
		"currency": currency,
		"payment_method": method,
		"record_kind": record_kind,
	}
	return row, problems, reconcilable


def _parse_report(content, account, verified_section_totals=None):
	"""Read a Meta billing export into rows, findings and totals.

	─── WHY THIS REPORTS INSTEAD OF REFUSING ─────────────────────────────────────

	Before, the first bad row raised and the operator saw one opaque message:
	"the CSV contains an invalid transaction on line 42". Every other problem
	in the file — and there is usually more than one — was hidden behind it,
	and the reason never reached the screen, because the route answered every
	failure with the same "import rejected".

	Now the parser READS the whole file and reports what it found: a finding
	per problem, each with a code, a line number and a plain sentence.

	─── WHY THE IMPORT IS STILL ALL-OR-NOTHING ───────────────────────────────────

	A file whose rows do not reconcile with its own section totals is not
	partially imported. Half a billing cycle is worse than none: the stored
	evidence would no longer add up to the file the operator holds, and the
	shortfall would be invisible. So the findings BLOCK the import; they do
	not narrow it.
	"""
	try:
		text = content.decode("utf-8-sig")
	except UnicodeDecodeError:
		frappe.throw("The Meta billing file must be a UTF-8 CSV export.")
	try:
		csv_rows = list(csv.reader(io.StringIO(text, newline="")))
	except csv.Error:
		frappe.throw("The Meta billing CSV could not be read.")
	if len(csv_rows) > MAX_IMPORT_ROWS + 1000:
		frappe.throw("This billing report has too many lines to import in one batch.")
	current_export_headers = [
		row for row in csv_rows
		if [cell.strip().casefold() for cell in row[:5]] == [
			"date", "transaction id", "payment method", "amount", "currency",
		]
	]
	if current_export_headers:
		normalized = _normalize_meta_invoice_summary(csv_rows, account)
		parsed = _parse_report(
			normalized["bytes"],
			account,
			verified_section_totals=normalized["section_totals_verified"],
		)
		parsed["sha256"] = hashlib.sha256(content).hexdigest()
		parsed["problems"] = _merge_problems(normalized["problems"], parsed.get("problems", []))
		parsed["report_row_count"] = normalized["source_row_count"]
		return parsed
	problems = []
	report_period_values = []
	transaction_date_values = []
	for raw_row in csv_rows:
		cells = [cell.strip() for cell in raw_row]
		if len(cells) == 1 and cells[0].casefold().startswith("billing report:"):
			report_period_values.append(cells[0].split(":", 1)[1].strip())
		elif len(cells) == 4 and _looks_like_date(cells[0]):
			transaction_date_values.append(cells[0])
	if len(report_period_values) > 1:
		problems.append(_problem("file", "MULTIPLE_PERIODS", "The Meta billing file contains more than one billing-report period."))
	report_period_text = report_period_values[0] if report_period_values else ""
	date_order = _infer_day_first([report_period_text, *transaction_date_values])
	report_period = None
	if report_period_text:
		try:
			report_period = _parse_report_period(report_period_text, date_order)
		except (ValueError, InvalidOperation, TypeError):
			problems.append(_problem("file", "INVALID_PERIOD", "The billing-report period could not be read."))

	file_account = ""
	method = ""
	section_totals = defaultdict(Decimal)
	section_total_seen = set()
	rows = []
	refused_reconcilable_rows = []
	unreconcilable_count = 0
	for row_number, raw_row in enumerate(csv_rows, start=1):
		cells = [cell.strip() for cell in raw_row]
		if not any(cells):
			continue
		if len(cells) == 1 and cells[0].casefold().startswith("account:"):
			match = META_ACCOUNT_PATTERN.fullmatch(cells[0].split(":", 1)[1].strip())
			if not match:
				problems.append(_problem("file", "ACCOUNT_MISMATCH", "The Meta billing file does not identify a valid ad account.", row_number))
			elif file_account:
				problems.append(_problem("file", "ACCOUNT_MISMATCH", "The Meta billing file identifies more than one ad account.", row_number))
			else:
				file_account = match.group(1)
			continue
		if len(cells) == 1 and cells[0].casefold().startswith("payment method:"):
			method = cells[0].split(":", 1)[1].strip()
			if not method:
				problems.append(_problem("file", "EMPTY_PAYMENT_METHOD_SECTION", "The Meta billing file has an empty payment-method section.", row_number))
			continue
		if len(cells) >= 4 and [cell.casefold() for cell in cells[:4]] == ["date", "transaction id", "amount", "currency"]:
			if not method:
				problems.append(_problem("file", "MISSING_PAYMENT_METHOD_SECTION", "A Meta transaction section is missing its payment method.", row_number))
			continue
		if len(cells) >= 4 and cells[1].casefold() == "total amount billed":
			if not method:
				problems.append(_problem("file", "SECTION_TOTAL_MISSING", "A Meta report total is missing its payment-method section.", row_number))
				continue
			try:
				amount = _csv_amount(cells[2])
				currency = _currency_code(cells[3])
			except (ValueError, InvalidOperation, TypeError, frappe.ValidationError):
				problems.append(_problem("file", "INVALID_TOTAL", "A Meta report total could not be read.", row_number))
				continue
			section_totals[(method.casefold(), currency)] += amount
			section_total_seen.add((method.casefold(), currency))
			continue
		if len(cells) == 4 and method:
			# A four-cell row under a payment method IS a transaction, even
			# when its date cannot be read. Ignoring it would drop money the
			# file's own total counts.
			if len(rows) >= MAX_IMPORT_ROWS:
				frappe.throw("This billing report has too many transaction rows to import in one batch.")
			parsed_row, row_problems, reconcilable = _read_transaction(
				cells, row_number, date_order, report_period, account, method, bool(method)
			)
			problems.extend(row_problems)
			if row_problems:
				# The row is refused. If its amount could still be READ, it
				# stays in the file's own total, and the section is reconciled
				# against it.
				if reconcilable:
					refused_reconcilable_rows.append(parsed_row)
				else:
					unreconcilable_count += 1
				continue
			rows.append(parsed_row)

	if not file_account or file_account != _account_digits(account.external_account_id):
		problems.append(_problem("file", "ACCOUNT_MISMATCH", "The CSV account ID does not match the selected Meta ad account."))
	if not rows:
		problems.append(_problem("file", "MISSING_TRANSACTION_ROWS", "The Meta billing CSV contains no transaction rows."))
	if not section_total_seen:
		problems.append(_problem("file", "SECTION_TOTAL_MISSING", "The Meta billing CSV is missing its section totals."))

	# ─── THE DUPLICATES FIRST ───────────────────────────────────────────────────
	#
	# A transaction that appears twice in the file is ONE transaction. The
	# section check counts it once, exactly as the file's own total does.
	# A duplicate whose VALUES differ is a conflict, and it is counted
	# separately so that a bad duplicate stays visible as a transaction-ID
	# conflict instead of being hidden by a second, misleading section-total
	# failure.
	unique_rows = []
	by_key = {}
	conflicts = []
	duplicate_rows = []
	duplicate_in_file_count = 0
	rows_for_totals = []
	for row in rows:
		previous = by_key.get(row["transaction_key"])
		if previous is None:
			by_key[row["transaction_key"]] = row
			unique_rows.append(row)
			rows_for_totals.append(row)
		elif _row_signature(previous) == _row_signature(row):
			duplicate_in_file_count += 1
			duplicate_rows.append(row)
		else:
			conflicts.append({"row_number": row["row_number"], "code": "DUPLICATE_ID_DIFFERS"})
			rows_for_totals.append(row)

	section_totals_verified = False
	if verified_section_totals is not None:
		section_totals_verified = verified_section_totals
	elif unreconcilable_count:
		problems.append(_problem(
			"file",
			"SECTION_TOTAL_UNVERIFIED",
			"Some transaction rows could not be read, so the file's section totals could not be verified.",
		))
	else:
		section_rows = defaultdict(list)
		for row in rows_for_totals:
			section_rows[(row["payment_method"].casefold(), row["currency"])].append(_source_amount(row["amount"]))
		# A row that was refused for a reason other than its amount — a date
		# outside the period, a currency that is not this account's — is
		# still part of the file's own total, so the section is reconciled
		# against it. Without this, the refused row would look like money
		# missing from the file.
		for row in refused_reconcilable_rows:
			section_rows[(row["payment_method"].casefold(), row["currency"])].append(_source_amount(row["amount"]))
		mismatch = False
		for key, amounts in section_rows.items():
			if key not in section_total_seen or sum(amounts, Decimal(0)) != section_totals[key]:
				problems.append(_problem("file", "SECTION_TOTAL_MISMATCH", "The Meta billing CSV transaction rows do not match its section total."))
				mismatch = True
				break
		if not mismatch and any(key not in section_rows for key in section_total_seen):
			problems.append(_problem("file", "SECTION_TOTAL_MISMATCH", "The Meta billing CSV contains a total without transaction rows."))
			mismatch = True
		section_totals_verified = not mismatch

	kinds = import_rules.IMPORT_RECORD_KINDS
	counts = {kind: sum(1 for row in unique_rows if row["record_kind"] == kind) for kind in kinds}
	totals = {}
	for kind in kinds:
		amounts_for_kind = [
			import_rules.kind_total_magnitude(kind, _source_amount(row["amount"]))
			for row in unique_rows if row["record_kind"] == kind
		]
		totals[kind] = format(sum(amounts_for_kind, Decimal(0)), "f")
	start_date = min((row["transaction_date"] for row in unique_rows), default="")
	end_date = max((row["transaction_date"] for row in unique_rows), default="")
	currencies = {row["currency"] for row in unique_rows}
	if len(currencies) > 1:
		problems.append(_problem("file", "MIXED_CURRENCIES", "A single Meta ad account report cannot mix currencies."))
	return {
		"rows": unique_rows,
		"report_row_count": len(rows),
		"duplicate_in_file_count": duplicate_in_file_count,
		"duplicate_rows": duplicate_rows,
		"conflicts": conflicts,
		"counts": counts,
		"totals": totals,
		"currency": rows[0]["currency"] if rows else "",
		"start_date": start_date,
		"end_date": end_date,
		"sha256": hashlib.sha256(content).hexdigest(),
		"problems": problems,
		"section_totals_verified": section_totals_verified,
	}


def _merge_problems(*groups):
	"""Merge finding lists, keeping the first report of each distinct problem."""
	seen = set()
	merged = []
	for group in groups:
		for problem in group or []:
			key = (problem.get("scope"), problem.get("code"), problem.get("row_number"))
			if key in seen:
				continue
			seen.add(key)
			merged.append(problem)
	return merged


def _normalize_meta_invoice_summary(csv_rows, account):
	"""Normalize Meta's current invoice-summary layout to the checked section format.

	Returns a dict with the normalized bytes, the findings, the number of
	transaction rows the FILE held, and whether the file's own section totals
	were verified.

	─── WHY THE NORMALIZER REPORTS AND DOES NOT REFUSE ───────────────────────────

	The normalizer used to raise on the first bad row, so the operator never
	reached the rest of the file. Now it reads the whole file, reports what it
	could not read, and passes on the rows it could.

	─── WHY THE FILE'S OWN TOTALS ARE STILL CHECKED ──────────────────────────────

	A section total is the file's claim about its own rows. The check survives:
	a row whose AMOUNT could not be read makes the section unverifiable, and
	that is reported as such rather than as a mismatch — because the mismatch
	would be a CONSEQUENCE of the unreadable row, not an independent fault.
	"""
	main_header = ["date", "transaction id", "payment method", "amount", "currency"]
	header_indexes = [
		index for index, row in enumerate(csv_rows)
		if [cell.strip().casefold() for cell in row[:5]] == main_header
	]
	if len(header_indexes) != 1:
		frappe.throw("The Meta invoice-summary CSV must contain exactly one payment table.")
	header_index = header_indexes[0]
	problems = []

	file_accounts = []
	for row in csv_rows[:header_index]:
		for cell in row:
			match = re.fullmatch(r"\s*account:\s*(.*?)\s*", cell, re.IGNORECASE)
			if match:
				account_match = META_ACCOUNT_PATTERN.fullmatch(match.group(1))
				if not account_match:
					problems.append(_problem("file", "ACCOUNT_MISMATCH", "The Meta invoice-summary CSV has an invalid ad account ID."))
				else:
					file_accounts.append(account_match.group(1))
	if len(file_accounts) != 1 or file_accounts[0] != _account_digits(account.external_account_id):
		problems.append(_problem("file", "ACCOUNT_MISMATCH", "The CSV account ID does not match the selected Meta ad account."))

	transaction_date_values = [
		row[0].strip() for row in csv_rows[header_index + 1:]
		if row and _looks_like_date(row[0])
	]
	date_order = _infer_day_first(transaction_date_values)
	# The only row-method table is also the header used to detect this export
	# format, so seed its section before walking the rows that follow it.
	current_section = {"mode": "row_method", "method": "", "rows": [], "amounts": [], "unreadable_amounts": False, "total": None}
	sections = [current_section]
	pending_method = ""
	source_row_count = 0
	section_totals_verified = True

	for row_number, raw_row in enumerate(csv_rows[header_index + 1:], start=header_index + 2):
		cells = [cell.strip() for cell in raw_row]
		if not any(cells):
			continue

		if len(cells) == 1:
			label = cells[0].casefold()
			if label == "meta ads payment":
				if current_section and current_section["total"] is None:
					problems.append(_problem("file", "SECTION_TOTAL_MISSING", "The Meta invoice-summary CSV is missing a section total.", row_number))
					section_totals_verified = False
				current_section = None
				pending_method = ""
				continue
			if label.startswith("payment method:"):
				if current_section and current_section["total"] is None:
					problems.append(_problem("file", "SECTION_TOTAL_MISSING", "The Meta invoice-summary CSV is missing a section total.", row_number))
					section_totals_verified = False
				pending_method = cells[0].split(":", 1)[1].strip()
				if not pending_method:
					problems.append(_problem("file", "EMPTY_PAYMENT_METHOD_SECTION", "The Meta invoice-summary CSV has an empty payment method.", row_number))
				current_section = None
				continue
			if label.startswith(("vat rate:", "vat amount:")):
				continue

		row_header = [cell.casefold() for cell in cells]
		if row_header[:5] == main_header:
			if current_section and current_section["total"] is None:
				problems.append(_problem("file", "SECTION_TOTAL_MISSING", "The Meta invoice-summary CSV is missing a section total.", row_number))
				section_totals_verified = False
			current_section = {"mode": "row_method", "method": "", "rows": [], "amounts": [], "unreadable_amounts": False, "total": None}
			sections.append(current_section)
			pending_method = ""
			continue
		if row_header[:4] == ["date", "transaction id", "amount", "currency"]:
			if current_section and current_section["total"] is None:
				problems.append(_problem("file", "SECTION_TOTAL_MISSING", "The Meta invoice-summary CSV is missing a section total.", row_number))
				section_totals_verified = False
			if not pending_method:
				problems.append(_problem("file", "MISSING_PAYMENT_METHOD_SECTION", "A Meta invoice-summary transaction table is missing its payment method.", row_number))
			current_section = {"mode": "fixed_method", "method": pending_method, "rows": [], "amounts": [], "unreadable_amounts": False, "total": None}
			sections.append(current_section)
			pending_method = ""
			continue

		total_cells = None
		if len(cells) >= 5 and cells[2].casefold() == "total amount billed":
			total_cells = (cells[3], cells[4])
		elif len(cells) >= 4 and cells[1].casefold() == "total amount billed":
			total_cells = (cells[2], cells[3])
		if total_cells is not None:
			if not current_section or current_section["total"] is not None:
				problems.append(_problem("file", "UNSUPPORTED_ROW", "The Meta invoice-summary CSV has an unexpected total.", row_number))
				continue
			try:
				total = _csv_amount(total_cells[0])
				currency = _currency_code(total_cells[1])
			except (ValueError, InvalidOperation, TypeError, frappe.ValidationError):
				problems.append(_problem("file", "INVALID_TOTAL", "The Meta invoice-summary CSV has an invalid total.", row_number))
				current_section["total"] = None
				current_section = None
				continue
			section_amount = sum(current_section["amounts"], Decimal(0))
			if currency != account.currency:
				problems.append(_problem("file", "TOTAL_CURRENCY_MISMATCH", "A Meta invoice-summary section total is in another currency.", row_number))
				section_totals_verified = False
			elif current_section["unreadable_amounts"]:
				problems.append(_problem(
					"file",
					"SECTION_TOTAL_UNVERIFIED",
					"Some transaction rows could not be read, so the file's section totals could not be verified.",
					row_number,
				))
				section_totals_verified = False
			elif total != section_amount:
				problems.append(_problem("file", "SECTION_TOTAL_MISMATCH", "The Meta invoice-summary section total does not match its transaction rows.", row_number))
				section_totals_verified = False
			current_section["total"] = total
			current_section["currency"] = currency
			current_section = None
			continue

		if current_section is not None:
			# A row whose SHAPE is right is a transaction, even when its date
			# cannot be read. Ignoring it would drop money the file's own
			# total counts.
			if current_section["mode"] == "row_method":
				shape_ok = len(cells) == 5 and not any(cells[5:])
			else:
				shape_ok = len(cells) == 4 and not any(cells[4:])
		else:
			shape_ok = False
		if _looks_like_date(cells[0]) or shape_ok:
			if current_section is None:
				problems.append(_problem("file", "UNSUPPORTED_ROW", "A Meta invoice-summary transaction appears outside a payment table.", row_number))
				continue
			if current_section["mode"] == "row_method":
				if len(cells) < 5 or any(cells[5:]):
					problems.append(_problem("file", "UNSUPPORTED_ROW", "The Meta invoice-summary transaction has an invalid shape.", row_number))
					continue
				method, amount_value, currency_value = cells[2], cells[3], cells[4]
			else:
				if len(cells) < 4 or any(cells[4:]):
					problems.append(_problem("file", "UNSUPPORTED_ROW", "The Meta invoice-summary transaction has an invalid shape.", row_number))
					continue
				method, amount_value, currency_value = current_section["method"], cells[2], cells[3]
			source_row_count += 1
			parsed_row, row_problems, reconcilable = _read_invoice_summary_row(
				cells, row_number, date_order, account, method, amount_value, currency_value
			)
			problems.extend(row_problems)
			if reconcilable:
				# The amount still counts towards the file's own section total,
				# even when the row itself cannot be stored: the file counted
				# it, and the check is against the FILE.
				current_section["amounts"].append(parsed_row["amount"])
				if not row_problems:
					current_section["rows"].append(parsed_row)
			else:
				current_section["unreadable_amounts"] = True
			continue

		problems.append(_problem("file", "UNSUPPORTED_ROW", "The Meta invoice-summary CSV has an unsupported row.", row_number))

	if current_section and current_section["total"] is None:
		problems.append(_problem("file", "SECTION_TOTAL_MISSING", "The Meta invoice-summary CSV is missing a section total."))
		section_totals_verified = False
	if not sections or not any(section["rows"] for section in sections):
		problems.append(_problem("file", "MISSING_TRANSACTION_ROWS", "The Meta billing CSV contains no transaction rows."))

	normalized_rows = [[f"Account: {file_accounts[0]}"]] if file_accounts else []
	for section in sections:
		by_method = {}
		for row in section["rows"]:
			by_method.setdefault(row["method"], []).append(row)
		for method, method_rows in by_method.items():
			normalized_rows.append([f"Payment method: {method}"])
			normalized_rows.append(["Date", "Transaction ID", "Amount", "Currency"])
			for row in method_rows:
				normalized_rows.append([
					row["date"], row["transaction_id"], _canonical_amount(row["amount"]), row["currency"],
				])
			total = sum((row["amount"] for row in method_rows), Decimal(0))
			normalized_rows.append(["", "Total amount billed", _canonical_amount(total), method_rows[0]["currency"]])

	output = io.StringIO(newline="")
	csv.writer(output).writerows(normalized_rows)
	return {
		"bytes": output.getvalue().encode("utf-8"),
		"problems": problems,
		"source_row_count": source_row_count,
		"section_totals_verified": section_totals_verified,
	}


def _read_invoice_summary_row(cells, row_number, date_order, account, method, amount_value, currency_value):
	"""Read one row of Meta's current invoice-summary layout."""
	problems = []
	reconcilable = True

	transaction_date = None
	try:
		transaction_date = _parse_date(cells[0], date_order)
	except (ValueError, InvalidOperation, TypeError):
		problems.append(_problem("row", "INVALID_DATE", "The transaction date could not be read.", row_number))

	transaction_id = ""
	try:
		transaction_id = _text(cells[1], "Transaction ID", 140)
	except (ValueError, InvalidOperation, TypeError, frappe.ValidationError):
		transaction_id = ""
	if not transaction_id or transaction_id.casefold() == "nan":
		problems.append(_problem("row", "MISSING_TRANSACTION_ID", "The transaction ID is empty.", row_number))
		transaction_id = ""

	amount = None
	try:
		amount = _csv_amount(amount_value)
	except (ValueError, InvalidOperation, TypeError):
		problems.append(_problem("row", "INVALID_AMOUNT", "The amount is not a plain decimal.", row_number))
		reconcilable = False
	if amount is not None and amount == 0:
		problems.append(_problem("row", "ZERO_AMOUNT", "The transaction amount is zero.", row_number))
		amount = None
		reconcilable = False

	currency = ""
	try:
		currency = _currency_code(currency_value)
	except (ValueError, InvalidOperation, TypeError, frappe.ValidationError):
		problems.append(_problem("row", "INVALID_CURRENCY", "The currency could not be read.", row_number))
		reconcilable = False
	if currency and currency != account.currency:
		# The currency was READ, so the section can still be reconciled against
		# this row. The row itself is refused: it is not this account's money.
		problems.append(_problem("row", "CURRENCY_MISMATCH", "The transaction currency does not match the selected Meta ad account.", row_number))

	method = method or ""
	if not method:
		problems.append(_problem("row", "MISSING_PAYMENT_METHOD", "The transaction has no payment method.", row_number))
		reconcilable = False

	return {
		"date": transaction_date.strftime("%d/%m/%Y") if date_order is True and transaction_date else (
			transaction_date.isoformat() if transaction_date else cells[0]
		),
		"transaction_id": transaction_id,
		"amount": amount,
		"currency": currency,
		"method": method,
	}, problems, reconcilable


def _record_kind(method, amount):
	"""Name the kind of one billing row.

	The rule itself lives in `meta_ads_import`, which is PURE and is mirrored
	by the web application. A reversal is a reversal whatever its sign: the
	old inference called a negative reversal a Refund and a positive one
	Unclassified, and both were wrong.
	"""
	return import_rules.classify_record_kind(method, amount)


def _problem(scope, code, detail, row_number=None):
	"""One finding, in the shape both the server and the application know."""
	record = {"scope": scope, "code": code, "detail": detail}
	if row_number is not None:
		record["row_number"] = row_number
	return record


def _problems_summary(problems):
	"""The one-line reason an import was refused."""
	if not problems:
		return ""
	rows = [problem for problem in problems if problem["scope"] == "row"]
	files = [problem for problem in problems if problem["scope"] == "file"]
	parts = []
	if files:
		parts.append(f"{len(files)} file problem{'s' if len(files) != 1 else ''}")
	if rows:
		lines = ", ".join(str(problem.get("row_number", "?")) for problem in rows[:5])
		parts.append(f"{len(rows)} invalid row{'s' if len(rows) != 1 else ''} (line {lines})")
	return "; ".join(parts)


def _problem_sentences(problems, limit=3):
	"""The first few findings, in plain sentences."""
	sentences = []
	for problem in problems[:limit]:
		if problem.get("row_number") is not None:
			sentences.append(f"Line {problem['row_number']}: {problem['detail']}")
		else:
			sentences.append(problem["detail"])
	return " ".join(sentences)


def _existing_matches(existing, row):
	try:
		return (
			str(existing.transaction_date) == row["transaction_date"]
			and _canonical_amount(_source_amount(existing.amount)) == row["amount"]
			and existing.currency == row["currency"]
			and (existing.payment_method or "").casefold() == row["payment_method"].casefold()
			and existing.record_kind == row["record_kind"]
		)
	except (InvalidOperation, TypeError, ValueError):
		return False


def _row_signature(row):
	return (
		row["transaction_date"], row["amount"], row["currency"], row["payment_method"].casefold(), row["record_kind"],
	)


def _csv_amount(value):
	raw = value.strip()
	if CSV_GROUPED_AMOUNT_PATTERN.fullmatch(raw):
		raw = raw.replace(",", "")
	if not CSV_AMOUNT_PATTERN.fullmatch(raw):
		raise ValueError("Amount is not a plain decimal.")
	amount = Decimal(raw)
	if not amount.is_finite():
		raise ValueError("Amount is not finite.")
	return amount


def _source_amount(value):
	return _csv_amount(str(value))


def _canonical_amount(value):
	return format(value.normalize(), "f")


def _infer_day_first(values):
	orders = set()
	for value in values:
		if not value:
			continue
		match = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)", value)
		if not match:
			continue
		first, second = int(match.group(1)), int(match.group(2))
		if first > 12 and second <= 12:
			orders.add(True)
		elif second > 12 and first <= 12:
			orders.add(False)
	if len(orders) > 1:
		frappe.throw("The Meta billing CSV mixes day-first and month-first dates.")
	return next(iter(orders), None)


def _parse_report_period(value, day_first):
	match = META_REPORT_PERIOD_PATTERN.fullmatch(value)
	if not match:
		frappe.throw("The Meta billing report period is not in a supported format.")
	try:
		start = _parse_date(match.group(1), day_first)
		end = _parse_date(match.group(2), day_first)
	except ValueError:
		frappe.throw("The Meta billing report period has an ambiguous or invalid date format.")
	if start > end:
		frappe.throw("The Meta billing report period ends before it starts.")
	return start, end


def _parse_date(value, day_first=None):
	value = value.strip()
	if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", value):
		patterns = ("%d/%m/%Y",) if day_first is True else (("%m/%d/%Y",) if day_first is False else ("%d/%m/%Y", "%m/%d/%Y"))
	else:
		patterns = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")
	parsed = []
	for pattern in patterns:
		try:
			candidate = datetime.strptime(value, pattern).date()
			if candidate not in parsed:
				parsed.append(candidate)
		except ValueError:
			continue
	if len(parsed) == 1:
		return parsed[0]
	if len(parsed) > 1:
		raise ValueError("Date is ambiguous.")
	raise ValueError("Date is not supported.")


def _looks_like_date(value):
	return bool(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9} \d{1,2}, \d{4}|\d{1,2} [A-Za-z]{3,9} \d{4}", value.strip()))


def _account_digits(value):
	match = META_ACCOUNT_PATTERN.fullmatch(str(value or "").strip())
	return match.group(1) if match else ""


def _profile_names(customer):
	name = frappe.db.get_value("Customer", customer, "customer_name") if customer else ""
	return {"customer_name": name or customer or ""}


def _unassigned_account_label(account):
	return f"Unassigned account-level — {account.account_name or account.external_account_id}"


def _import_customer_name(row, customer_names, account_by_name):
	if row.billing_customer_snapshot:
		return customer_names.get(row.billing_customer_snapshot) or row.billing_customer_snapshot
	account = account_by_name.get(row.ad_account)
	if account:
		return _unassigned_account_label(account)
	return f"Unassigned account-level — {row.external_account_id}"


def _profile_snapshot(profile, effective_date):
	if not profile:
		return {
			"profile": "",
			"customer": "",
			"relationship_kind": "",
			"fee_percent": "",
			"invoice_currency": "",
			"page_assets": "[]",
		}
	return {
		"profile": profile.name,
		"customer": profile.customer,
		"relationship_kind": profile.relationship_kind or "Customer",
		"fee_percent": _fee_percent_for_date(profile.as_dict(), effective_date),
		"invoice_currency": profile.invoice_currency,
		"page_assets": _page_assets_snapshot(profile),
	}


def _page_assets_snapshot(profile):
	if not profile:
		return "[]"
	return json.dumps([{
		"page_id": row.page_id or "",
		"page_name": row.page_name or "",
		"instagram_account_id": row.instagram_account_id or "",
		"instagram_username": row.instagram_username or "",
		"served_customer": row.served_customer or "",
	} for row in profile.get("page_assets") or []], ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal_text(value):
	return format(_decimal(value), "f")


def _decimal_map(values):
	return {key: format(value, "f") for key, value in sorted(values.items())}
