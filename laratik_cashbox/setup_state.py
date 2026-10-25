"""Schema validation for the non-financial first-run setup draft."""

from __future__ import annotations

import json
from typing import Any


MAX_SETUP_JSON_BYTES = 32_768
COMPANY_FIELDS = (
	"company_name",
	"abbr",
	"country",
	"default_currency",
	"chart_of_accounts",
	"fiscal_year_name",
	"fiscal_year_start_date",
	"fiscal_year_end_date",
)
COMPANY_FIELD_LIMITS = {
	"company_name": 140,
	"abbr": 10,
	"country": 80,
	"default_currency": 10,
	"chart_of_accounts": 100,
	"fiscal_year_name": 80,
	"fiscal_year_start_date": 20,
	"fiscal_year_end_date": 20,
}
ACCOUNT_MAPPING_KEYS = (
	"company_cash",
	"custodian_cash",
	"advances",
	"funds_in_transit",
	"receivables",
	"payables",
	"salary_payable",
	"personal_reimbursements_payable",
	"founder_funding_payable",
	"service_revenue",
	"software_subscriptions",
	"hosting",
	"advertising",
	"training",
	"gpu_computing",
	"operating_expenses",
	"exchange_gain",
	"exchange_loss",
)
ACCOUNT_MAPPING_ROOT_TYPES = {
	"company_cash": "Asset",
	"custodian_cash": "Asset",
	"advances": "Asset",
	"funds_in_transit": "Asset",
	"receivables": "Asset",
	"payables": "Liability",
	"salary_payable": "Liability",
	"personal_reimbursements_payable": "Liability",
	"founder_funding_payable": "Liability",
	"service_revenue": "Income",
	"software_subscriptions": "Expense",
	"hosting": "Expense",
	"advertising": "Expense",
	"training": "Expense",
	"gpu_computing": "Expense",
	"operating_expenses": "Expense",
	"exchange_gain": "Income",
	"exchange_loss": "Expense",
}


def _object(value: Any, label: str) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise ValueError(f"{label} must be an object")
	return value


def _text(value: Any, label: str, maximum: int = 140) -> str:
	if value is None:
		return ""
	if not isinstance(value, str):
		raise ValueError(f"{label} must be text")
	value = value.strip()
	if len(value) > maximum:
		raise ValueError(f"{label} is too long")
	return value


def normalize_setup_state(raw: Any) -> dict[str, Any]:
	"""Parse, bound, and normalize setup values before storing them.

	Only configuration fields are accepted. This store intentionally has no
	amount, transaction, employee, customer, supplier, or credential fields.
	"""
	if isinstance(raw, str):
		if len(raw.encode("utf-8")) > MAX_SETUP_JSON_BYTES:
			raise ValueError("setup draft is too large")
		try:
			raw = json.loads(raw)
		except (TypeError, ValueError) as exc:
			raise ValueError("setup draft must be valid JSON") from exc

	state = _object(raw, "setup draft")
	unknown = set(state) - {"version", "company", "account_mappings", "currency_confirmed", "tax_review_acknowledged"}
	if unknown:
		raise ValueError(f"unsupported setup fields: {', '.join(sorted(unknown))}")
	if state.get("version", 1) != 1:
		raise ValueError("unsupported setup draft version")

	company_input = _object(state.get("company", {}), "company")
	unknown_company_fields = set(company_input) - set(COMPANY_FIELDS)
	if unknown_company_fields:
		raise ValueError(f"unsupported company fields: {', '.join(sorted(unknown_company_fields))}")
	company = {
		field: _text(company_input.get(field, ""), field, COMPANY_FIELD_LIMITS[field])
		for field in COMPANY_FIELDS
	}

	mappings_input = _object(state.get("account_mappings", {}), "account mappings")
	unknown_mappings = set(mappings_input) - set(ACCOUNT_MAPPING_KEYS)
	if unknown_mappings:
		raise ValueError(f"unsupported account mappings: {', '.join(sorted(unknown_mappings))}")
	mappings = {
		key: _text(mappings_input.get(key, ""), key, 140)
		for key in ACCOUNT_MAPPING_KEYS
	}

	currency_confirmed = state.get("currency_confirmed", False)
	if not isinstance(currency_confirmed, bool):
		raise ValueError("currency_confirmed must be true or false")

	tax_review_acknowledged = state.get("tax_review_acknowledged", False)
	if not isinstance(tax_review_acknowledged, bool):
		raise ValueError("tax_review_acknowledged must be true or false")

	clean = {
		"version": 1,
		"company": company,
		"account_mappings": mappings,
		"currency_confirmed": currency_confirmed,
		"tax_review_acknowledged": tax_review_acknowledged,
	}
	if len(json.dumps(clean, ensure_ascii=False).encode("utf-8")) > MAX_SETUP_JSON_BYTES:
		raise ValueError("setup draft is too large")
	return clean
