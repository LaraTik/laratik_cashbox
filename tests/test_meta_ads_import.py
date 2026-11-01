"""The vocabulary and rules of a Meta billing import.

The module under test is PURE, so these tests need no site and no Frappe
fake: the rules are exercised directly.
"""

from decimal import Decimal

import pytest

from laratik_cashbox import meta_ads_import as import_rules


# ─── THE RECORD KINDS ─────────────────────────────────────────────────────────

def test_the_vocabulary_names_five_kinds():
	assert import_rules.IMPORT_RECORD_KINDS == (
		"Payment",
		"Refund",
		"Credit",
		"Reversal",
		"Unclassified",
	)


def test_three_kinds_reduce_the_settlement_position():
	# A refund, a credit and a reversal all move money back towards LaraTik.
	assert import_rules.SETTLEMENT_REDUCING_KINDS == ("Refund", "Credit", "Reversal")


# ─── THE RULE ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method", ["Visa", "Paypal", "Bank Transfer", "Mastercard", "Amex"])
def test_a_payment_method_names_a_payment(method):
	assert import_rules.classify_record_kind(method, Decimal("10.00")) == "Payment"


@pytest.mark.parametrize("method", ["Ad Credit", "Promotional Credit", "Account Credit"])
def test_a_named_credit_names_a_credit(method):
	assert import_rules.classify_record_kind(method, Decimal("0.15")) == "Credit"


def test_a_refund_by_name_is_a_refund():
	assert import_rules.classify_record_kind("Refund", Decimal("20.00")) == "Refund"


def test_a_negative_amount_is_a_refund_when_the_method_says_nothing():
	# The export's sign convention for a refund is not fixed, so the sign is
	# the evidence when the name is not.
	assert import_rules.classify_record_kind("Visa", Decimal("-20.00")) == "Refund"


def test_an_unknown_method_is_unclassified():
	assert import_rules.classify_record_kind("Money Transfer", Decimal("7.00")) == "Unclassified"


def test_an_empty_method_is_unclassified():
	assert import_rules.classify_record_kind("", Decimal("7.00")) == "Unclassified"


# ─── THE REVERSAL ────────────────────────────────────────────────────────────

def test_a_positive_reversal_is_a_reversal():
	# Before, this was Unclassified: the file said exactly what the row was,
	# and the answer hid it inside the "needs review" bucket.
	assert import_rules.classify_record_kind("Reversal", Decimal("30.00")) == "Reversal"


def test_a_negative_reversal_is_a_reversal_and_not_a_refund():
	# Before, this was Refund — which says LaraTik refunded money, when what
	# happened is that Meta undid a charge.
	assert import_rules.classify_record_kind("Reversal", Decimal("-30.00")) == "Reversal"


def test_a_reversal_outranks_a_credit_token():
	# "Ad Credit Reversal" contains a credit token and says "reversal" first.
	assert import_rules.classify_record_kind("Ad Credit Reversal", Decimal("-2.50")) == "Reversal"


def test_a_credit_card_is_a_payment_and_not_a_credit():
	# The bare word "credit" must not turn a card into a credit.
	assert import_rules.classify_record_kind("Credit Card", Decimal("40.00")) == "Payment"
	assert import_rules.classify_record_kind("Visa Credit Card", Decimal("40.00")) == "Payment"


def test_a_bare_credit_word_is_a_credit():
	assert import_rules.classify_record_kind("Credit", Decimal("5.00")) == "Credit"


def test_the_rule_table_is_ordered_and_names_the_same_kinds():
	# The table is the SPEC. It and the implementation must not drift, and
	# the ORDER is the precedence.
	assert [rule["id"] for rule in import_rules.RECORD_KIND_RULES] == [
		"reversal_by_name",
		"credit_by_name",
		"credit_by_word",
		"refund_by_name",
		"refund_by_sign",
		"payment_by_name",
		"unclassified",
	]
	kinds = {rule["kind"] for rule in import_rules.RECORD_KIND_RULES}
	assert kinds == set(import_rules.IMPORT_RECORD_KINDS)


# ─── THE TOTALS ──────────────────────────────────────────────────────────────

def test_a_reducing_kind_is_stated_as_a_magnitude():
	assert import_rules.kind_total_magnitude("Reversal", Decimal("-30.00")) == Decimal("30.00")
	assert import_rules.kind_total_magnitude("Refund", Decimal("-12.00")) == Decimal("12.00")
	assert import_rules.kind_total_magnitude("Credit", Decimal("-1.50")) == Decimal("1.50")


def test_an_increasing_kind_keeps_its_sign():
	assert import_rules.kind_total_magnitude("Payment", Decimal("30.00")) == Decimal("30.00")
	assert import_rules.kind_total_magnitude("Unclassified", Decimal("-4.00")) == Decimal("-4.00")


# ─── THE SETTLEMENT FORMULA ──────────────────────────────────────────────────

def test_payments_add_to_the_settlement_position():
	assert import_rules.settlement_net(100) == Decimal("100")


def test_a_refund_reduces_the_settlement_position():
	assert import_rules.settlement_net(100, refunds=10) == Decimal("90")


def test_a_credit_reduces_the_settlement_position():
	assert import_rules.settlement_net(100, credits=5) == Decimal("95")


def test_a_reversal_reduces_the_settlement_position():
	assert import_rules.settlement_net(100, reversals=25) == Decimal("75")


def test_all_three_reductions_apply_together():
	assert import_rules.settlement_net(100, refunds=10, credits=5, reversals=25) == Decimal("60")


def test_an_unclassified_record_keeps_its_own_sign():
	# Its direction is not known, and guessing one would be inventing a
	# settlement.
	assert import_rules.settlement_net(100, unclassified=7) == Decimal("107")
	assert import_rules.settlement_net(100, unclassified=-12) == Decimal("88")


# ─── THE VOCABULARIES ────────────────────────────────────────────────────────

def test_the_row_codes_are_unique():
	assert len(set(import_rules.ROW_VALIDATION_CODES)) == len(import_rules.ROW_VALIDATION_CODES)


def test_the_file_codes_are_unique():
	assert len(set(import_rules.FILE_VALIDATION_CODES)) == len(import_rules.FILE_VALIDATION_CODES)


def test_the_row_and_file_codes_do_not_overlap():
	# A code names one thing. A code that meant "an invalid amount" on a row
	# and "an invalid amount in the file" would need two labels to stay
	# unambiguous.
	assert not set(import_rules.ROW_VALIDATION_CODES) & set(import_rules.FILE_VALIDATION_CODES)


def test_the_row_statuses_name_the_three_outcomes():
	assert import_rules.IMPORT_ROW_STATUSES == ("new", "duplicate", "conflict")
