"""Tests for the currency policy — the rule that stops two currencies being added.

─── WHY THESE RUN WITHOUT FRAPPE ───────────────────────────────────────────────

The policy is a decision about two strings, so it is tested with two strings:
no site, no database, no `import frappe`. That is the same reason the rollup
was split out of its controller — a rule that needs a live site is a rule
that is only checked when somebody happens to be looking.

Run them with:

    cd laratik_cashbox && python3 -m pytest tests/test_currency_policy.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "laratik_cashbox",
    ),
)

from currency_policy import (  # noqa: E402
    ACCOUNT_CURRENCY_FIELD,
    COMPANY_CURRENCY_FIELD,
    box_currency,
    row_is_sumable,
)


class TestBoxCurrency:
    def test_the_account_is_the_authority(self):
        # A cash account can be in a currency the company does not report in.
        # The ACCOUNT is where the money is; the company is only the fallback.
        assert box_currency("EUR", "USD") == "EUR"

    def test_the_company_is_the_fallback_when_the_account_names_none(self):
        assert box_currency("", "USD") == "USD"
        assert box_currency(None, "USD") == "USD"

    def test_neither_naming_one_leaves_it_unknown(self):
        assert box_currency("", "") is None
        assert box_currency(None, None) is None

    def test_whitespace_is_not_a_currency(self):
        assert box_currency("   ", "   ") is None


class TestRowIsSumable:
    def test_a_row_in_the_box_currency_is_sumable(self):
        assert row_is_sumable("USD", "USD") is True

    def test_a_row_in_another_currency_is_REFUSED(self):
        # THE DEFECT THIS PINS: the audit's F7 said a row inserted from Desk
        # with `currency = "EUR"` would be summed into a USD balance with no
        # conversion and no warning. This is the line that refuses it.
        assert row_is_sumable("EUR", "USD") is False

    def test_an_unknown_box_currency_cannot_be_mismatched(self):
        # There is nothing to compare against. The CALLER decides what an
        # unknown means; this rule does not invent one.
        assert row_is_sumable("EUR", None) is True
        assert row_is_sumable("EUR", "") is True

    def test_a_row_with_unknown_currency_is_unverified_for_a_known_box(self):
        # Legacy rows with no proven denomination must not enter a complete
        # currency-specific total.
        assert row_is_sumable("", "USD") is False
        assert row_is_sumable(None, "USD") is False

    def test_the_comparison_is_exact(self):
        # Case and whitespace are not currency codes. "usd" is not "USD", and
        # a comparison that lowercases would treat a typo as a match.
        assert row_is_sumable("usd", "USD") is False
        assert row_is_sumable(" USD ", "USD") is True


class TestTheFieldNames:
    def test_the_account_field_is_account_currency(self):
        # NOT `currency`: the ERPNext Account doctype has no field by that
        # name, and a fix written from the audit's wording would read a field
        # that does not exist. The pinned v16 schema
        # (cashbox-app/fixtures/erpnext-v16/account.json) is the evidence.
        assert ACCOUNT_CURRENCY_FIELD == "account_currency"

    def test_the_company_field_is_default_currency(self):
        assert COMPANY_CURRENCY_FIELD == "default_currency"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
