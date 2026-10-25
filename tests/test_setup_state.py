"""The setup draft stores configuration, never financial or personal records."""

import pytest

from laratik_cashbox.setup_state import (
	ACCOUNT_MAPPING_KEYS,
	ACCOUNT_MAPPING_ROOT_TYPES,
	COMPANY_FIELDS,
	normalize_setup_state,
)


def test_empty_setup_state_has_only_known_fields():
	state = normalize_setup_state({})

	assert state["version"] == 1
	assert tuple(state["company"]) == COMPANY_FIELDS
	assert tuple(state["account_mappings"]) == ACCOUNT_MAPPING_KEYS
	assert state["currency_confirmed"] is False
	assert state["tax_review_acknowledged"] is False
	assert tuple(ACCOUNT_MAPPING_ROOT_TYPES) == ACCOUNT_MAPPING_KEYS
	assert set(ACCOUNT_MAPPING_ROOT_TYPES.values()) == {"Asset", "Liability", "Income", "Expense"}


def test_company_and_account_mapping_inputs_are_normalized():
	state = normalize_setup_state(
		{
			"company": {
				"company_name": "  LaraTik GmbH  ",
				"country": "Germany",
				"default_currency": "EUR",
				"chart_of_accounts": "SKR03 mit Kontonummern",
			},
			"account_mappings": {"company_cash": "  Kasse - LT  "},
			"currency_confirmed": True,
			"tax_review_acknowledged": True,
		}
	)

	assert state["company"]["company_name"] == "LaraTik GmbH"
	assert state["company"]["country"] == "Germany"
	assert state["company"]["default_currency"] == "EUR"
	assert state["account_mappings"]["company_cash"] == "Kasse - LT"
	assert state["currency_confirmed"] is True
	assert state["tax_review_acknowledged"] is True


@pytest.mark.parametrize(
	"payload",
	[
		"not json",
		{"amount": "100.00"},
		{"company": {"tax_id": "invented"}},
		{"account_mappings": {"bank_account": "Made-up Bank"}},
		{"tax_review_acknowledged": "yes"},
		{"currency_confirmed": "yes"},
	],
)
def test_unknown_or_malformed_values_are_refused(payload):
	with pytest.raises(ValueError):
		normalize_setup_state(payload)


def test_large_draft_is_refused():
	with pytest.raises(ValueError, match="too large"):
		normalize_setup_state(" " * 40_000)
