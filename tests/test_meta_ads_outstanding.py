"""What the customers still owe — the rule, and the endpoint that reads it.

─── WHY THIS TEST EXISTS ─────────────────────────────────────────────────────────

The Meta Ads Overview shows "outstanding customer receivables, when available
from ERPNext". That figure went wrong twice, in two different ways, and both
times a MOCKED test passed:

  1. The query read `base_outstanding_amount` — a column ERPNext 16 does not
     have. On the real site the query raised `Unknown column`, so the figure
     could never render at all. No mocked test could see it, because a mock
     answers whatever the fixture says.

  2. The figure it did read was labelled the INVOICE currency, while ERPNext
     keeps the outstanding in the PARTY ACCOUNT's currency — two different
     currencies, and the label was on the wrong one.

Both are the same class of defect: **a fact about ERPNext that was asserted
rather than verified**. So this test holds three things:

  1. The RULE, in its pure form, over every branch — the rule is ADR-017's,
     the one `/position` already uses, and it must never guess a rate.
  2. The ENDPOINT — that it reads columns ERPNext has, that it labels the
     money in the currency ERPNext keeps it in, and that it withholds a total
     it could only state for part of the book.
  3. The CONVERSION — that a partial total is never returned. A total that
     silently omits an invoice understates what the customers owe, and nothing
     on the tile would say so.
"""

from __future__ import annotations

import importlib
import sys
import types
from decimal import Decimal

import pytest

from laratik_cashbox import outstanding_policy as P


MODULE = "laratik_cashbox.meta_ads_invoice_api"

COMPANY = "Example GmbH"


# ─── THE RULE, IN ITS PURE FORM ─────────────────────────────────────────────────


def test_the_party_account_currency_in_the_company_currency_converts_by_one():
    # A receivable account in the COMPANY currency, on an invoice billed in
    # something else. The outstanding is ALREADY in company currency, and
    # applying the invoice's conversion_rate here is the bug this rule exists
    # to stop: that rate is the TRANSACTION -> company rate, and it has
    # nothing to do with this amount.
    result = P.to_company_currency(
        "1000.00", "EUR", "USD", "EUR", "0.9"
    )

    assert result.ok
    assert result.amount == Decimal("1000.00")
    assert result.factor == Decimal(1)
    assert result.basis == P.BASIS_IDENTITY


def test_a_same_currency_invoice_uses_the_rate_which_is_one():
    # When the billing currency IS the party account currency, the rule uses
    # the conversion rate — and in a same-currency company that rate is 1, so
    # the amount is unchanged. The ORDER of the checks is what makes the case
    # above safe.
    result = P.to_company_currency(
        "1000.00", "EUR", "EUR", "EUR", "1.0"
    )

    assert result.ok
    assert result.amount == Decimal("1000.00")
    assert result.factor == Decimal("1")
    assert result.basis == P.BASIS_CONVERSION_RATE


def test_the_billing_currency_in_the_party_account_converts_by_the_conversion_rate():
    result = P.to_company_currency(
        "1000.00", "USD", "USD", "EUR", "0.9"
    )

    assert result.ok
    assert result.amount == Decimal("900.00")
    assert result.factor == Decimal("0.9")
    assert result.basis == P.BASIS_CONVERSION_RATE


def test_a_zero_rate_is_never_used_as_a_factor():
    # A missing rate must not become 0, and must not become 1 either.
    for rate in ("0", "0.0", None, ""):
        result = P.to_company_currency("1000.00", "USD", "USD", "EUR", rate)

        assert not result.ok
        assert result.reason == P.REASON_MISSING_CONVERSION_RATE
        assert result.amount is None


def test_an_absent_amount_is_not_a_zero():
    for amount in (None, ""):
        result = P.to_company_currency(amount, "EUR", "EUR", "EUR", "1.0")

        assert not result.ok
        assert result.reason == P.REASON_MISSING_OUTSTANDING_AMOUNT


def test_a_blank_amount_is_not_a_zero():
    # `Decimal("")` is not a number, and a blank field is an amount nobody
    # could read — not an invoice the client owes nothing on.
    for blank in ("", "   "):
        result = P.to_company_currency(blank, "EUR", "EUR", "EUR", "1.0")

        assert not result.ok
        assert result.reason == P.REASON_MISSING_OUTSTANDING_AMOUNT


def test_a_non_numeric_amount_is_not_a_zero():
    result = P.to_company_currency("not a number", "EUR", "EUR", "EUR", "1.0")

    assert not result.ok
    assert result.reason == P.REASON_NON_NUMERIC_OUTSTANDING


def test_a_missing_party_account_currency_is_unresolved():
    # Without it, the currency the amount is denominated in is unknown, so the
    # amount cannot be grouped and cannot be converted.
    result = P.to_company_currency("1000.00", "", "EUR", "EUR", "1.0")

    assert not result.ok
    assert result.reason == P.REASON_MISSING_PARTY_ACCOUNT_CURRENCY


def test_a_third_currency_is_unresolved_and_never_guessed():
    # Billed in USD, receivable account in GBP, company in EUR: two rates are
    # needed and the invoice carries one. The answer is "unresolved", never a
    # number.
    result = P.to_company_currency("1000.00", "GBP", "USD", "EUR", "0.9")

    assert not result.ok
    assert result.reason == P.REASON_NO_RATE_FOR_PARTY_ACCOUNT_CURRENCY
    assert result.amount is None


def test_a_missing_company_currency_is_unresolved():
    result = P.to_company_currency("1000.00", "GBP", "USD", "", "0.9")

    assert not result.ok
    assert result.reason == P.REASON_MISSING_COMPANY_CURRENCY


def test_the_currency_comparison_ignores_case_and_padding():
    result = P.to_company_currency("1000.00", " eur ", "EUR", "EUR", "1.0")

    assert result.ok
    assert result.basis == P.BASIS_CONVERSION_RATE


def test_the_amount_is_decimal_and_not_a_float():
    # 0.1 + 0.2 in binary floating point is 0.30000000000000004. Money that
    # rounds wrong is money that reconciles wrong.
    result = P.to_company_currency("0.1", "EUR", "EUR", "EUR", "1.0")

    assert result.amount == Decimal("0.10")
    assert str(result.amount) == "0.10"


def test_convert_all_refuses_a_partial_total():
    converted, unresolved, total = P.convert_all([
        {
            "outstanding": "1000.00",
            "party_account_currency": "EUR",
            "invoice_currency": "EUR",
            "company_currency": "EUR",
            "conversion_rate": "1.0",
        },
        {
            "outstanding": "500.00",
            "party_account_currency": "GBP",
            "invoice_currency": "USD",
            "company_currency": "EUR",
            "conversion_rate": "0.9",
        },
    ])

    assert len(converted) == 1
    assert len(unresolved) == 1
    assert unresolved[0][1] == P.REASON_NO_RATE_FOR_PARTY_ACCOUNT_CURRENCY
    # The total is None, NOT 1000.00. A partial total is the dangerous shape.
    assert total is None


def test_convert_all_states_the_total_when_nothing_is_unresolved():
    converted, unresolved, total = P.convert_all([
        {
            "outstanding": "1000.00",
            "party_account_currency": "EUR",
            "invoice_currency": "EUR",
            "company_currency": "EUR",
            "conversion_rate": "1.0",
        },
        {
            "outstanding": "250.50",
            "party_account_currency": "EUR",
            "invoice_currency": "EUR",
            "company_currency": "EUR",
            "conversion_rate": "1.0",
        },
    ])

    assert len(converted) == 2
    assert unresolved == []
    assert total == Decimal("1250.50")


def test_convert_all_over_an_empty_book_states_a_zero():
    # Nothing outstanding is a zero. Nothing READABLE is a zero too — and the
    # two are told apart by the unresolved list, not by the total.
    converted, unresolved, total = P.convert_all([])

    assert converted == []
    assert unresolved == []
    assert total == Decimal(0)


# ─── THE ENDPOINT ───────────────────────────────────────────────────────────────


class Row(dict):
    """A row that answers to attribute access, like a Frappe document does."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as error:  # pragma: no cover - a guard, not a path
            raise AttributeError(name) from error


class FakeDoc(Row):
    def check_permission(self, permission):
        return None


def _matches(row, filters):
    for key, condition in (filters or {}).items():
        value = getattr(row, key, None)
        if isinstance(condition, (list, tuple)):
            operator = condition[0]
            if operator == "in":
                if value not in condition[1]:
                    return False
            elif operator == ">":
                if not float(value) > float(condition[1]):
                    return False
            else:
                raise AssertionError(f"the fake site does not understand {operator!r}")
        elif value != condition:
            return False
    return True


@pytest.fixture
def api(monkeypatch):
    """The invoice API, on a site with a Company and some Sales Invoices."""
    saved_frappe = sys.modules.get("frappe")
    saved_utils = sys.modules.get("frappe.utils")
    saved_requests = sys.modules.get("requests")

    fake = types.ModuleType("frappe")
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.only_for = lambda role: (_ for _ in ()).throw(PermissionError(role))
    fake.session = Row(user="Administrator")

    state = Row(
        company=Row(name=COMPANY, default_currency="EUR"),
        accounts={},
        invoices=[],
        queries=[],
    )

    def get_all(doctype, filters=None, fields=None, order_by=None, limit_page_length=None):
        state.queries.append((doctype, fields))
        if doctype == "Sales Invoice":
            rows = [row for row in state.invoices if _matches(row, filters)]
        elif doctype == "Account":
            names = set(filters.get("name", [None, None])[1])
            rows = [
                Row(name=name, account_currency=currency)
                for name, currency in state.accounts.items()
                if name in names
            ]
        else:
            rows = []
        if limit_page_length is not None:
            rows = rows[:limit_page_length]
        return rows

    def get_value(doctype, name, field):
        if doctype == "Company":
            return getattr(state.company, field, None)
        return None

    def exists(doctype, name):
        return doctype == "Company" and name == COMPANY

    fake.get_all = get_all
    fake.db = Row(get_value=get_value, exists=exists)
    fake.get_doc = lambda doctype, name: FakeDoc({"doctype": doctype, "name": name})

    fake.utils = types.ModuleType("frappe.utils")
    fake.utils.getdate = lambda value: value
    fake.utils.now_datetime = lambda: "2026-11-02 09:00:00"

    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = RuntimeError

    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", fake.utils)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    sys.modules.pop(MODULE, None)
    module = importlib.import_module(MODULE)
    yield module, state
    sys.modules.pop(MODULE, None)
    for name, saved in (
        ("frappe", saved_frappe),
        ("frappe.utils", saved_utils),
        ("requests", saved_requests),
    ):
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


def _invoice(name, **overrides):
    values = {
        "name": name,
        "company": COMPANY,
        # Only a SUBMITTED invoice is a receivable. The endpoint filters on
        # it, so the fake has to carry it.
        "docstatus": 1,
        "customer": "Example Customer",
        "currency": "EUR",
        "debit_to": "Debtors - Example GmbH",
        "outstanding_amount": "1000.00",
        "conversion_rate": "1.0",
        "posting_date": "2026-10-01",
        "due_date": "2026-10-31",
    }
    values.update(overrides)
    return Row(**values)


def test_the_endpoint_reads_no_column_erpnext_does_not_have(api):
    # The defect this whole file exists for: a query for `base_outstanding_amount`
    # raised on the real site, and only on the real site.
    module, state = api
    state.accounts["Debtors - Example GmbH"] = "EUR"
    state.invoices.append(_invoice("SINV-0001"))

    module.get_outstanding_receivables(COMPANY)

    for doctype, fields in state.queries:
        if doctype != "Sales Invoice":
            continue
        assert "base_outstanding_amount" not in (fields or [])


def test_the_endpoint_states_the_total_when_every_invoice_can_be_stated(api):
    module, state = api
    state.accounts["Debtors - Example GmbH"] = "EUR"
    state.invoices.append(_invoice("SINV-0001", outstanding_amount="1000.00"))
    state.invoices.append(_invoice("SINV-0002", outstanding_amount="250.50"))

    result = module.get_outstanding_receivables(COMPANY)

    assert result["available"] is True
    assert result["company_currency"] == "EUR"
    assert result["total"] == {"EUR": "1250.50"}
    assert result["total_complete"] is True
    assert result["total_reason"] == ""
    assert result["unresolved_count"] == 0
    assert result["invoice_count"] == 2


def test_the_endpoint_labels_the_money_in_the_currency_erpnext_keeps_it_in(api):
    # ERPNext keeps the outstanding in the PARTY ACCOUNT's currency. Calling
    # that the invoice currency is a label on a number that means something
    # else.
    module, state = api
    state.accounts["Debtors - Example GmbH"] = "EUR"
    state.invoices.append(_invoice("SINV-0001", currency="USD", conversion_rate="0.9"))

    result = module.get_outstanding_receivables(COMPANY)

    # ERPNext keeps the outstanding in the RECEIVABLE ACCOUNT's currency — EUR
    # here — and the account is already in the company's currency, so the
    # amount is 1000.00 EUR. The old code multiplied it by the invoice's
    # 0.9 USD->EUR rate and read 900.00: the invoice's rate for an amount
    # that was never in USD.
    assert result["by_account_currency"] == {"EUR": "1000.00"}
    assert result["total"] == {"EUR": "1000.00"}
    assert result["invoices"][0]["party_account_currency"] == "EUR"
    assert result["invoices"][0]["conversion_basis"] == P.BASIS_IDENTITY


def test_the_endpoint_withholds_a_total_it_could_only_state_in_part(api):
    module, state = api
    state.accounts["Debtors - Example GmbH"] = "EUR"
    state.accounts["Debtors - GBP - Example GmbH"] = "GBP"
    state.invoices.append(_invoice("SINV-0001", outstanding_amount="1000.00"))
    state.invoices.append(
        _invoice(
            "SINV-0002",
            debit_to="Debtors - GBP - Example GmbH",
            currency="USD",
            outstanding_amount="500.00",
            conversion_rate="0.9",
        )
    )

    result = module.get_outstanding_receivables(COMPANY)

    # A partial total would be {"EUR": "1000.00"} — smaller than the truth,
    # and the tile would not say so.
    assert result["total"] == {}
    assert result["total_complete"] is False
    assert "1 invoice could not be stated" in result["total_reason"]
    assert "withheld" in result["total_reason"]
    assert result["unresolved_count"] == 1
    assert result["unresolved"][0]["name"] == "SINV-0002"
    assert result["unresolved"][0]["reason"] == P.REASON_NO_RATE_FOR_PARTY_ACCOUNT_CURRENCY


def test_the_account_breakdown_survives_a_missing_conversion_rate(api):
    # The breakdown by account currency needs the amount and the account
    # currency, but NOT a company conversion. An invoice that is missing an
    # exchange rate is out of the COMPANY total and still in the ACCOUNT
    # breakdown — because there, it can be stated.
    module, state = api
    state.accounts["Debtors - Example GmbH"] = "EUR"
    state.accounts["Debtors - USD - Example GmbH"] = "USD"
    state.invoices.append(_invoice("SINV-0001", outstanding_amount="1000.00"))
    state.invoices.append(
        _invoice(
            "SINV-0002",
            debit_to="Debtors - USD - Example GmbH",
            currency="USD",
            outstanding_amount="500.00",
            conversion_rate="0",
        )
    )

    result = module.get_outstanding_receivables(COMPANY)

    assert result["total"] == {}
    assert result["total_complete"] is False
    assert result["unresolved"][0]["reason"] == P.REASON_MISSING_CONVERSION_RATE
    # The breakdown does NOT need a company conversion: the amount is known
    # and the account currency is known, so both invoices are in it.
    assert result["by_account_currency"] == {"EUR": "1000.00", "USD": "500.00"}
    assert result["by_account_currency_complete"] is True
    assert result["unresolved_count"] == 1


def test_the_endpoint_withholds_the_breakdown_when_an_account_currency_is_unreadable(api):
    # An invoice pointing at a receivable account that was not read back — a
    # deleted account, or one the company filter did not return. The amount's
    # currency is unknown, so the invoice cannot be GROUPED and the breakdown
    # is withheld rather than shown as a partial figure.
    module, state = api
    state.accounts["Debtors - Example GmbH"] = "EUR"
    state.invoices.append(_invoice("SINV-0001", outstanding_amount="1000.00"))
    state.invoices.append(
        _invoice("SINV-0002", debit_to="Debtors - Missing - Example GmbH")
    )

    result = module.get_outstanding_receivables(COMPANY)

    assert result["by_account_currency"] == {}
    assert result["by_account_currency_complete"] is False
    assert result["unresolved_count"] == 1
    assert result["unresolved"][0]["reason"] == P.REASON_MISSING_PARTY_ACCOUNT_CURRENCY


def test_the_endpoint_reads_the_account_currencies_in_one_query(api):
    # The N+1 this avoids: one query for every receivable account's currency,
    # not one per invoice.
    module, state = api
    state.accounts["Debtors - Example GmbH"] = "EUR"
    state.accounts["Debtors - GBP - Example GmbH"] = "GBP"
    state.invoices.append(_invoice("SINV-0001"))
    state.invoices.append(
        _invoice("SINV-0002", debit_to="Debtors - GBP - Example GmbH")
    )

    module.get_outstanding_receivables(COMPANY)

    account_queries = [q for q in state.queries if q[0] == "Account"]
    assert len(account_queries) == 1
