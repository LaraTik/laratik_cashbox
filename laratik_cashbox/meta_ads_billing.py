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
		"unclassified_count": parsed["counts"]["Unclassified"],
		"payment_total": parsed["totals"]["Payment"],
		"refund_total": parsed["totals"]["Refund"],
		"credit_total": parsed["totals"]["Credit"],
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
		"unclassified_total": parsed["totals"]["Unclassified"],
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
	for raw in payments:
		account = account_by_name.get(raw.ad_account)
		if not account or raw.external_account_id != account.external_account_id:
			unreadable_count += 1
			continue
		try:
			amount = _source_amount(raw.amount)
			currency = _currency_code(raw.currency)
			if currency != account.currency or raw.record_kind not in ("Payment", "Refund", "Credit", "Unclassified"):
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
		elif raw.record_kind == "Refund":
			group["refund"][currency] += abs(amount)
			group["refund_count"] += 1
		elif raw.record_kind == "Credit":
			group["credit"][currency] += abs(amount)
		else:
			group["unclassified"][currency] += amount
			group["unclassified_count"] += 1

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

	return {
		"company": company_name,
		"since": start.isoformat(),
		"until": end.isoformat(),
		"complete": complete,
		"truncated": accounts_truncated or rows_truncated,
		"unreadable_count": unreadable_count,
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
			new_rows.append(row)
			continue
		if _existing_matches(existing, row):
			duplicate_count += 1
		else:
			conflicts.append({"row_number": row["row_number"], "code": "TRANSACTION_DIFFERS"})

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
			"fee_percent": _fee_percent_for_date(profile.as_dict(), parsed["end_date"]) if profile else "",
			"invoice_currency": profile.invoice_currency if profile else "",
			"start_date": parsed["start_date"],
			"end_date": parsed["end_date"],
			"currency": parsed["currency"],
			"row_count": parsed["report_row_count"],
			"payment_count": parsed["counts"]["Payment"],
			"refund_count": parsed["counts"]["Refund"],
			"credit_count": parsed["counts"]["Credit"],
			"unclassified_count": parsed["counts"]["Unclassified"],
			"payment_total": parsed["totals"]["Payment"],
			"refund_total": parsed["totals"]["Refund"],
			"credit_total": parsed["totals"]["Credit"],
			"unclassified_total": parsed["totals"]["Unclassified"],
			"duplicate_count": duplicate_count,
			"new_row_count": len(new_rows),
			"conflict_count": len(conflicts),
			"conflicts": conflicts[:50],
			"methods": sorted({row["payment_method"] for row in parsed["rows"]}),
			"can_import": not conflicts,
			"payment_status_available": False,
		},
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


def _parse_report(content, account):
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
		parsed = _parse_report(normalized, account)
		parsed["sha256"] = hashlib.sha256(content).hexdigest()
		return parsed
	report_period_values = []
	transaction_date_values = []
	for raw_row in csv_rows:
		cells = [cell.strip() for cell in raw_row]
		if len(cells) == 1 and cells[0].casefold().startswith("billing report:"):
			report_period_values.append(cells[0].split(":", 1)[1].strip())
		elif len(cells) == 4 and _looks_like_date(cells[0]):
			transaction_date_values.append(cells[0])
	if len(report_period_values) > 1:
		frappe.throw("The Meta billing file contains more than one billing-report period.")
	report_period_text = report_period_values[0] if report_period_values else ""
	date_order = _infer_day_first([report_period_text, *transaction_date_values])
	report_period = _parse_report_period(report_period_text, date_order) if report_period_text else None

	file_account = ""
	method = ""
	section_totals = defaultdict(Decimal)
	section_total_seen = set()
	rows = []
	for row_number, raw_row in enumerate(csv_rows, start=1):
		cells = [cell.strip() for cell in raw_row]
		if not any(cells):
			continue
		if len(cells) == 1 and cells[0].casefold().startswith("account:"):
			match = META_ACCOUNT_PATTERN.fullmatch(cells[0].split(":", 1)[1].strip())
			if not match or file_account:
				frappe.throw("The Meta billing file does not identify exactly one valid ad account.")
			file_account = match.group(1)
			continue
		if len(cells) == 1 and cells[0].casefold().startswith("payment method:"):
			method = cells[0].split(":", 1)[1].strip()
			if not method:
				frappe.throw("The Meta billing file has an empty payment-method section.")
			continue
		if len(cells) >= 4 and [cell.casefold() for cell in cells[:4]] == ["date", "transaction id", "amount", "currency"]:
			if not method:
				frappe.throw("A Meta transaction section is missing its payment method.")
			continue
		if len(cells) >= 4 and cells[1].casefold() == "total amount billed":
			if not method:
				frappe.throw("A Meta report total is missing its payment-method section.")
			amount = _csv_amount(cells[2])
			currency = _currency_code(cells[3])
			section_totals[(method.casefold(), currency)] += amount
			section_total_seen.add((method.casefold(), currency))
			continue
		if len(cells) == 4 and method and _looks_like_date(cells[0]):
			if len(rows) >= MAX_IMPORT_ROWS:
				frappe.throw("This billing report has too many transaction rows to import in one batch.")
			try:
				transaction_date = _parse_date(cells[0], date_order)
				if report_period and not report_period[0] <= transaction_date <= report_period[1]:
					raise ValueError("Transaction date is outside the billing-report period.")
				transaction_id = _text(cells[1], "Transaction ID", 140)
				amount = _csv_amount(cells[2])
				currency = _currency_code(cells[3])
				if not transaction_id or transaction_id.casefold() == "nan":
					raise ValueError("Transaction ID is empty.")
				if amount == 0:
					raise ValueError("Transaction amount is zero.")
			except (ValueError, InvalidOperation, TypeError):
				frappe.throw(f"The Meta billing CSV contains an invalid transaction on line {row_number}.")
			if currency != account.currency:
				frappe.throw("A transaction currency does not match the selected Meta ad account.")
			record_kind = _record_kind(method, amount)
			transaction_key = hashlib.sha256(
				f"{_account_digits(account.external_account_id)}:{transaction_id}".encode("utf-8")
			).hexdigest()
			parsed = {
				"row_number": row_number,
				"transaction_key": transaction_key,
				"transaction_date": transaction_date.isoformat(),
				"transaction_id": transaction_id,
				"amount": _canonical_amount(amount),
				"currency": currency,
				"payment_method": method,
				"record_kind": record_kind,
			}
			rows.append(parsed)

	if not file_account or file_account != _account_digits(account.external_account_id):
		frappe.throw("The CSV account ID does not match the selected Meta ad account.")
	if not rows:
		frappe.throw("The Meta billing CSV contains no transaction rows.")
	if not section_total_seen:
		frappe.throw("The Meta billing CSV is missing its section totals.")

	unique_rows = []
	by_key = {}
	conflicts = []
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
		else:
			conflicts.append({"row_number": row["row_number"], "code": "DUPLICATE_ID_DIFFERS"})
			# Preserve conflicting variants in the section check so a bad duplicate
			# remains visible as a transaction-ID conflict instead of being hidden
			# by a second, misleading section-total failure.
			rows_for_totals.append(row)

	section_rows = defaultdict(list)
	for row in rows_for_totals:
		section_rows[(row["payment_method"].casefold(), row["currency"])].append(_source_amount(row["amount"]))
	for key, amounts in section_rows.items():
		if key not in section_total_seen or sum(amounts, Decimal(0)) != section_totals[key]:
			frappe.throw("The Meta billing CSV transaction rows do not match its section total.")
	if any(key not in section_rows for key in section_total_seen):
		frappe.throw("The Meta billing CSV contains a total without transaction rows.")

	counts = {kind: sum(1 for row in unique_rows if row["record_kind"] == kind) for kind in ("Payment", "Refund", "Credit", "Unclassified")}
	totals = {}
	for kind in counts:
		amounts_for_kind = [
			abs(_source_amount(row["amount"])) if kind in ("Refund", "Credit") else _source_amount(row["amount"])
			for row in unique_rows if row["record_kind"] == kind
		]
		amount = sum(amounts_for_kind, Decimal(0))
		totals[kind] = format(amount, "f")
	start_date = min(row["transaction_date"] for row in unique_rows)
	end_date = max(row["transaction_date"] for row in unique_rows)
	if len({row["currency"] for row in unique_rows}) != 1:
		frappe.throw("A single Meta ad account report cannot mix currencies.")
	return {
		"rows": unique_rows,
		"report_row_count": len(rows),
		"duplicate_in_file_count": duplicate_in_file_count,
		"conflicts": conflicts,
		"counts": counts,
		"totals": totals,
		"currency": rows[0]["currency"],
		"start_date": start_date,
		"end_date": end_date,
		"sha256": hashlib.sha256(content).hexdigest(),
	}


def _normalize_meta_invoice_summary(csv_rows, account):
	"""Normalize Meta's current invoice-summary layout to the checked section format."""
	main_header = ["date", "transaction id", "payment method", "amount", "currency"]
	header_indexes = [
		index for index, row in enumerate(csv_rows)
		if [cell.strip().casefold() for cell in row[:5]] == main_header
	]
	if len(header_indexes) != 1:
		frappe.throw("The Meta invoice-summary CSV must contain exactly one payment table.")
	header_index = header_indexes[0]

	file_accounts = []
	for row in csv_rows[:header_index]:
		for cell in row:
			match = re.fullmatch(r"\s*account:\s*(.*?)\s*", cell, re.IGNORECASE)
			if match:
				account_match = META_ACCOUNT_PATTERN.fullmatch(match.group(1))
				if not account_match:
					frappe.throw("The Meta invoice-summary CSV has an invalid ad account ID.")
				file_accounts.append(account_match.group(1))
	if len(file_accounts) != 1 or file_accounts[0] != _account_digits(account.external_account_id):
		frappe.throw("The CSV account ID does not match the selected Meta ad account.")

	transaction_date_values = [
		row[0].strip() for row in csv_rows[header_index + 1:]
		if row and _looks_like_date(row[0])
	]
	date_order = _infer_day_first(transaction_date_values)
	# The only row-method table is also the header used to detect this export
	# format, so seed its section before walking the rows that follow it.
	current_section = {"mode": "row_method", "method": "", "rows": [], "total": None}
	sections = [current_section]
	pending_method = ""

	for row_number, raw_row in enumerate(csv_rows[header_index + 1:], start=header_index + 2):
		cells = [cell.strip() for cell in raw_row]
		if not any(cells):
			continue

		if len(cells) == 1:
			label = cells[0].casefold()
			if label == "meta ads payment":
				if current_section and current_section["total"] is None:
					frappe.throw("The Meta invoice-summary CSV is missing a section total.")
				current_section = None
				pending_method = ""
				continue
			if label.startswith("payment method:"):
				if current_section and current_section["total"] is None:
					frappe.throw("The Meta invoice-summary CSV is missing a section total.")
				pending_method = cells[0].split(":", 1)[1].strip()
				if not pending_method:
					frappe.throw("The Meta invoice-summary CSV has an empty payment method.")
				current_section = None
				continue
			if label.startswith(("vat rate:", "vat amount:")):
				continue

		row_header = [cell.casefold() for cell in cells]
		if row_header[:5] == main_header:
			if current_section and current_section["total"] is None:
				frappe.throw("The Meta invoice-summary CSV is missing a section total.")
			current_section = {"mode": "row_method", "method": "", "rows": [], "total": None}
			sections.append(current_section)
			pending_method = ""
			continue
		if row_header[:4] == ["date", "transaction id", "amount", "currency"]:
			if current_section and current_section["total"] is None:
				frappe.throw("The Meta invoice-summary CSV is missing a section total.")
			if not pending_method:
				frappe.throw("A Meta invoice-summary transaction table is missing its payment method.")
			current_section = {"mode": "fixed_method", "method": pending_method, "rows": [], "total": None}
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
				frappe.throw(f"The Meta invoice-summary CSV has an unexpected total on line {row_number}.")
			try:
				total = _csv_amount(total_cells[0])
				currency = _currency_code(total_cells[1])
			except (ValueError, InvalidOperation, TypeError):
				frappe.throw(f"The Meta invoice-summary CSV has an invalid total on line {row_number}.")
			section_rows = current_section["rows"]
			section_amount = sum((row["amount"] for row in section_rows), Decimal(0))
			if currency != account.currency or total != section_amount:
				frappe.throw("The Meta invoice-summary section total does not match its transaction rows.")
			current_section["total"] = total
			current_section["currency"] = currency
			current_section = None
			continue

		if _looks_like_date(cells[0]):
			if current_section is None:
				frappe.throw(f"A Meta invoice-summary transaction appears outside a payment table on line {row_number}.")
			if current_section["mode"] == "row_method":
				if len(cells) < 5 or any(cells[5:]):
					frappe.throw(f"The Meta invoice-summary transaction has an invalid shape on line {row_number}.")
				method, amount_value, currency_value = cells[2], cells[3], cells[4]
			else:
				if len(cells) < 4 or any(cells[4:]):
					frappe.throw(f"The Meta invoice-summary transaction has an invalid shape on line {row_number}.")
				method, amount_value, currency_value = current_section["method"], cells[2], cells[3]
			try:
				transaction_date = _parse_date(cells[0], date_order)
				transaction_id = _text(cells[1], "Transaction ID", 140)
				amount = _csv_amount(amount_value)
				currency = _currency_code(currency_value)
				method = _text(method, "Payment method", 140)
				if not transaction_id or transaction_id.casefold() == "nan":
					raise ValueError("Transaction ID is empty.")
				if amount == 0:
					raise ValueError("Transaction amount is zero.")
				if currency != account.currency or not method:
					raise ValueError("Transaction currency or payment method is invalid.")
			except (ValueError, InvalidOperation, TypeError):
				frappe.throw(f"The Meta invoice-summary CSV contains an invalid transaction on line {row_number}.")
			current_section["rows"].append({
				"date": transaction_date.strftime("%d/%m/%Y") if date_order is True else transaction_date.isoformat(),
				"transaction_id": transaction_id,
				"amount": amount,
				"currency": currency,
				"method": method,
			})
			continue

		frappe.throw(f"The Meta invoice-summary CSV has an unsupported row on line {row_number}.")

	if current_section and current_section["total"] is None:
		frappe.throw("The Meta invoice-summary CSV is missing a section total.")
	if not sections or not any(section["rows"] for section in sections):
		frappe.throw("The Meta invoice-summary CSV contains no transaction rows.")

	normalized_rows = [[f"Account: {file_accounts[0]}"]]
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
	return output.getvalue().encode("utf-8")


def _record_kind(method, amount):
	normalized = re.sub(r"[^a-z0-9]+", " ", method.casefold()).strip()
	if any(token in normalized for token in ("ad credit", "promotional credit", "account credit")):
		return "Credit"
	if "refund" in normalized or amount < 0:
		return "Refund"
	if any(token in normalized for token in (
		"paypal", "visa", "mastercard", "master card", "american express", "amex", "card",
		"bank", "debit", "sepa", "apple pay", "google pay", "klarna", "ideal", "unionpay", "discover", "jcb",
	)):
		return "Payment"
	return "Unclassified"


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
