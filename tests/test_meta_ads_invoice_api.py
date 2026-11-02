"""The customer invoice workspace, and the safe creation of a draft.

─── WHY THIS TEST EXISTS ─────────────────────────────────────────────────────────

The invoice workflow is the one place in this application where a number turns
into an accounting document. Everything it does wrong is wrong in a way a
customer finds:

  - a draft created for a period that was already invoiced is a DOUBLE
    invoice;
  - a draft created for a period whose policy was never decided is the
    application inventing an accounting policy;
  - a draft that is SUBMITTED is an accounting entry this application was
    explicitly told never to create;
  - an invoice that disagrees with the report is a reconciliation nobody can
    close.

So the tests here hold five things:

  1. The workspace names the charge, its currency, and its READINESS, and it
     says WHICH decision or WHICH missing datum is holding it back.
  2. A period that is not ready is REFUSED — the draft action is not a
     formality, and the refusal carries the reasons.
  3. A ready period creates a DRAFT, a batch, and its lines — and never
     submits anything.
  4. A period that was already invoiced is never invoiced again, and a
     period that OVERLAPS one is refused rather than merged.
  5. The charge follows the profile's CHARGE MODEL: a recharge carries the
     advertising cost and the fee, and a service-fee-only invoice carries the
     fee ALONE, because the customer's own money is not the customer's bill.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import date, datetime

import pytest


MODULE = "laratik_cashbox.meta_ads_invoice_api"

COMPANY = "Example GmbH"
ACCOUNT = "account-1"
RUN = "run-1"
CUSTOMER = "Example Customer"
PROFILE = "Live profile"
PAGE = "900000000000001"
RECEIVABLE = "Debtors - Example"

PERIOD_START = "2026-10-01"
PERIOD_END = "2026-10-31"


# ─── THE FAKE SITE ─────────────────────────────────────────────────────────────


class Row(dict):
    """A row like Frappe's `_dict`: a MAPPING that also answers attributes."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class FakeDoc(Row):
    """A document that can be written to and inserted.

    Frappe's Document stores a field when you assign to it, so this fake does
    the same: `doc.customer = "X"` sets the FIELD, not an attribute beside it.
    A fake that quietly kept the two apart would let a test pass while the
    document it checked was empty.
    """

    def __init__(self, values, site):
        super().__init__(values)
        object.__setattr__(self, "_site", site)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self[name] = value

    def check_permission(self, permission):
        return None

    def get_password(self, field):
        return "token"

    def set(self, fieldname, value):
        self[fieldname] = value

    def submit(self):
        """A submission would be recorded, so the test can say it never happened."""
        self["docstatus"] = 1
        self._site.submitted.append(self.get("name"))
        return self

    def insert(self, *args, **kwargs):
        doctype = self.get("doctype")
        self["name"] = f"{doctype}-new-{len(self._site.created.get(doctype, [])) + 1}"
        self._site.created.setdefault(doctype, []).append(self)
        return self


class FakeSite:
    """Just enough of a site to read the reports and write one draft."""

    def __init__(self):
        self.tables = {
            "LaraTik Meta Ads Account": [],
            "LaraTik Meta Ads Sync": [],
            "LaraTik Meta Ads Spend": [],
            "LaraTik Meta Ads Billing Profile": [],
            "LaraTik Meta Ads Page Asset": [],
            "LaraTik Meta Ads Invoice Batch": [],
            "LaraTik Meta Ads Invoice Line": [],
            "Customer": [],
            "Company": [],
            "Account": [],
            "Party Account": [],
            "GL Entry": [],
            "Currency Exchange": [],
            "Sales Invoice": [],
        }
        self.created = {}
        self.queries = []
        self.submitted = []
        #: ERPNext's own site settings, read by the readiness.
        self.settings = {
            "Accounts Settings": {
                "allow_stale": 0,
                "stale_days": 1,
                "allow_multi_currency_invoices_against_single_party_account": 0,
            },
        }

    def rows(self, doctype, filters=None, limit=None):
        self.queries.append((doctype, filters))
        found = [row for row in self.tables.get(doctype, []) if _matches(row, filters)]
        if limit is not None:
            found = found[:limit]
        return found


def _matches(row, filters):
    for key, condition in (filters or {}).items():
        value = getattr(row, key, None)
        if isinstance(condition, (list, tuple)):
            operator = condition[0]
            if operator == "in":
                if value not in condition[1]:
                    return False
            elif operator == "not in":
                if value in condition[1]:
                    return False
            elif operator == "<=":
                if not str(value) <= str(condition[1]):
                    return False
            elif operator == ">=":
                if not str(value) >= str(condition[1]):
                    return False
            elif operator == "between":
                low, high = condition[1]
                if not str(low) <= str(value) <= str(high):
                    return False
            else:
                raise AssertionError(f"the fake site does not understand {operator!r}")
        elif value != condition:
            return False
    return True


@pytest.fixture
def site(monkeypatch):
    saved_frappe = sys.modules.get("frappe")
    saved_utils = sys.modules.get("frappe.utils")
    saved_requests = sys.modules.get("requests")

    fake = types.ModuleType("frappe")
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.only_for = lambda role: (_ for _ in ()).throw(PermissionError(role))
    fake.session = Row(user="Administrator")

    state = FakeSite()

    def get_all(doctype, filters=None, fields=None, order_by=None, limit_page_length=None):
        return state.rows(doctype, filters, limit_page_length)

    def get_value(doctype, name, field):
        if doctype == "Currency" and field == "fraction_units":
            return {"EUR": 100, "USD": 100}.get(name)
        if doctype == "Sales Invoice":
            for row in state.tables.get("Sales Invoice", []):
                if getattr(row, "name", None) == name:
                    return getattr(row, field, None)
            return None
        if isinstance(name, dict):
            # Frappe's `get_value(doctype, filters, fieldname)` — the first row
            # that matches the filters.
            for row in state.tables.get(doctype, []):
                if _matches(row, name):
                    return getattr(row, field, None)
            return None
        for row in state.tables.get(doctype, []):
            if getattr(row, "name", None) == name:
                return getattr(row, field, None)
        return None

    def get_single_value(doctype, field):
        return state.settings.get(doctype, {}).get(field)

    def set_value(doctype, name, values, update_modified=False):
        for row in state.tables.get(doctype, []):
            if getattr(row, "name", None) == name:
                row.update(values)
                return

    def new_doc(doctype):
        return FakeDoc({"doctype": doctype}, state)

    def get_doc(*args, **kwargs):
        if len(args) == 1 and isinstance(args[0], dict):
            values = dict(args[0])
            values.setdefault("doctype", "Sales Invoice")
            return FakeDoc(values, state)
        doctype, name = args[0], args[1]
        for row in state.tables.get(doctype, []):
            if getattr(row, "name", None) == name:
                return FakeDoc(dict(row), state)
        return FakeDoc({"doctype": doctype, "name": name}, state)

    fake.get_all = get_all
    fake.get_value = get_value
    fake.db = Row(
        get_value=get_value,
        set_value=set_value,
        exists=lambda doctype, name: True,
        get_single_value=get_single_value,
    )
    fake.new_doc = new_doc
    fake.get_doc = get_doc
    fake.utils = types.ModuleType("frappe.utils")

    def parse_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

    fake.utils.getdate = parse_date
    fake.utils.now_datetime = lambda: datetime(2026, 11, 2, 9, 0, 0)
    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = RuntimeError
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", fake.utils)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    sys.modules.pop(MODULE, None)
    sys.modules.pop("laratik_cashbox.meta_ads_api", None)
    api = importlib.import_module(MODULE)
    api.TEST_SITE = state
    yield api, state
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


# ─── THE DATA ──────────────────────────────────────────────────────────────────


def _account():
    return Row(
        name=ACCOUNT,
        external_account_id="act_123",
        account_name="Example Ads",
        currency="EUR",
        timezone="Europe/Berlin",
        billing_profile=PROFILE,
        status="Active",
        last_sync_at="2026-10-31 08:00:00",
        last_sync_status="Complete",
        company=COMPANY,
    )


def _run():
    return Row(
        name=RUN,
        ad_account=ACCOUNT,
        external_account_id="act_123",
        fetched_at="2026-10-31 08:00:00",
        row_count=2,
        ad_insights_status="Complete",
        ad_row_count=2,
        ad_reconciliation_status="Matched",
        ad_reconciliation_difference="0.00",
        ad_message="",
        status="Complete",
        from_date=PERIOD_START,
        to_date=PERIOD_END,
        company=COMPANY,
    )


def _account_spend(spend_date, amount):
    return Row(
        name=f"daily-{spend_date}",
        level="Account",
        sync_run=RUN,
        ad_account=ACCOUNT,
        external_account_id="act_123",
        billing_profile=PROFILE,
        billing_customer_snapshot=CUSTOMER,
        relationship_kind_snapshot="Customer",
        fee_percent_snapshot="12.5",
        fee_status_snapshot="configured",
        invoice_currency_snapshot="USD",
        page_assets_snapshot="[]",
        spend_date=spend_date,
        amount=amount,
        currency="EUR",
        source="Meta Marketing API Insights",
        fetched_at="2026-10-31 08:00:00",
        company=COMPANY,
    )


def _ad_spend(ad_id, amount, fee_status="configured"):
    snapshot = {
        "page_id": PAGE,
        "page_name": "Example Page",
        "instagram_account_id": "ig-1",
        "instagram_username": "example",
        "served_customer": "",
    }
    return Row(
        name=f"ad-{ad_id}",
        level="Ad",
        sync_run=RUN,
        ad_account=ACCOUNT,
        external_account_id="act_123",
        billing_profile=PROFILE,
        billing_customer_snapshot=CUSTOMER,
        relationship_kind_snapshot="Customer",
        fee_percent_snapshot="12.5",
        fee_status_snapshot=fee_status,
        invoice_currency_snapshot="USD",
        page_asset_snapshot=json.dumps(snapshot),
        campaign_id="camp-1",
        campaign_name="Autumn",
        adset_id="set-1",
        adset_name="All",
        ad_id=ad_id,
        ad_name=f"Ad {ad_id}",
        creative_id="cre-1",
        creative_story_id="",
        page_id=PAGE,
        page_name="Example Page",
        instagram_actor_id="ig-1",
        attribution_status="Attributed",
        spend_date="2026-10-15",
        amount=amount,
        currency="EUR",
        source="Meta Marketing API Insights",
        fetched_at="2026-10-31 08:00:00",
        company=COMPANY,
    )


def _profile(**overrides):
    values = {
        "name": PROFILE,
        "customer": CUSTOMER,
        "company": COMPANY,
        "disabled": 0,
        "fee_is_configured": 1,
        "charge_model": "Recharge",
        "recognition_model": "Principal",
        "income_account": "Sales - Example",
        "invoice_item": "Meta Ads Recharge",
        "tax_treatment": "None",
        "tax_template": "",
        "fee_percent": "12.5",
        "relationship_kind": "Customer",
        "invoice_currency": "USD",
    }
    values.update(overrides)
    return Row(**values)


def _seed(state, **profile_overrides):
    state.tables["LaraTik Meta Ads Account"] = [_account()]
    state.tables["LaraTik Meta Ads Sync"] = [_run()]
    # The account-level spend must cover the ad-level spend: the ad rows are
    # a PART of the account's delivery, and a report that found them
    # exceeding it would withhold the billing — correctly.
    state.tables["LaraTik Meta Ads Spend"] = [
        _account_spend("2026-10-01", "100.00"),
        _account_spend("2026-10-02", "100.00"),
        _ad_spend("ad-1", "120.00"),
    ]
    state.tables["LaraTik Meta Ads Billing Profile"] = [_profile(**profile_overrides)]
    state.tables["Customer"] = [Row(name=CUSTOMER, customer_name="Example Customer Name")]
    # THE SITE FACTS THE READINESS READS.
    #
    # The customer is billed in USD, so the receivable it is booked against is
    # a USD account — the arrangement ERPNext requires, and the one a customer
    # billed in a foreign currency needs on the site.
    state.tables["Company"] = [
        Row(
            name=COMPANY,
            default_currency="EUR",
            default_receivable_account=RECEIVABLE,
        )
    ]
    state.tables["Account"] = [
        Row(name=RECEIVABLE, account_currency="USD"),
        Row(name="Sales - Example", account_currency="EUR"),
    ]
    state.tables["Party Account"] = []
    state.tables["GL Entry"] = []
    state.tables["Currency Exchange"] = [
        # The PRICE of the charge: the advertising cost into the billing
        # currency.
        Row(
            name="fx-1",
            from_currency="EUR",
            to_currency="USD",
            date="2026-10-31",
            exchange_rate="1.1",
            for_selling=1,
        ),
        # The INVOICE into the company's books: a different pair, pointing the
        # other way.
        Row(
            name="fx-2",
            from_currency="USD",
            to_currency="EUR",
            date="2026-10-31",
            exchange_rate="0.9",
            for_selling=1,
        ),
    ]


def _workspace(api):
    return api.get_invoice_workspace(COMPANY, PERIOD_START, PERIOD_END)


def _row(api, state):
    rows = _workspace(api)["rows"]
    assert len(rows) == 1
    return rows[0]


# ─── THE WORKSPACE ─────────────────────────────────────────────────────────────


def test_the_workspace_names_the_charge_the_customer_and_its_readiness(site):
    api, state = site
    _seed(state)

    row = _row(api, state)

    assert row["customer"] == CUSTOMER
    assert row["customer_name"] == "Example Customer Name"
    assert row["profile"] == PROFILE
    assert row["rechargeable_cost"] == {"EUR": "120.00"}
    assert row["service_fee"] == {"EUR": "15.00"}
    assert row["invoice_currency"] == "USD"
    assert row["fee_percent"] == "12.5"
    assert row["fee_status"] == "configured"
    assert row["charge_model"] == "Recharge"
    # €120.00 of advertising plus 12.5% of €120.00, at 1.1.
    assert row["invoice_amount"] == "148.50"
    assert row["readiness"] == "ready"
    assert row["readiness_reasons"] == []
    assert row["accounts"] == [{"name": ACCOUNT, "account_name": "Example Ads"}]
    assert row["page_assets"][0]["page_id"] == PAGE


def test_a_service_fee_only_invoice_carries_the_fee_alone(site):
    """The customer's own advertising spend is not the customer's bill."""
    api, state = site
    _seed(state, charge_model="Service fee only")

    row = _row(api, state)

    # 12.5% of €120.00 = €15.00, at 1.1 = $16.50. The €120.00 is the
    # customer's own money passing through, so it is NOT on the invoice.
    assert row["invoice_amount"] == "16.50"
    assert row["rechargeable_cost"] == {"EUR": "120.00"}


def test_an_undecided_charge_model_is_not_computed_and_not_assumed(site):
    api, state = site
    _seed(state, charge_model="")

    row = _row(api, state)

    assert row["invoice_amount"] is None
    assert row["readiness"] == "needs_approval"
    assert "charge_model_not_decided" in row["readiness_reasons"]


def test_an_undecided_recognition_model_waits_for_a_decision(site):
    api, state = site
    _seed(state, recognition_model="")

    row = _row(api, state)

    assert row["readiness"] == "needs_approval"
    assert "recognition_model_not_decided" in row["readiness_reasons"]


def test_a_fee_that_was_never_stated_is_not_a_zero_fee(site):
    api, state = site
    _seed(state, fee_is_configured=0)
    state.tables["LaraTik Meta Ads Spend"] = [
        _account_spend("2026-10-01", "100.00"),
        _account_spend("2026-10-02", "100.00"),
        _ad_spend("ad-1", "120.00", fee_status="unconfigured"),
    ]

    row = _row(api, state)

    assert row["fee_status"] == "unconfigured"
    assert row["readiness"] == "needs_approval"
    assert "fee_unconfigured" in row["readiness_reasons"]


def test_a_missing_income_account_holds_the_draft(site):
    api, state = site
    _seed(state, income_account="")

    row = _row(api, state)

    assert row["readiness"] == "needs_approval"
    assert "income_account_missing" in row["readiness_reasons"]


def test_a_missing_exchange_rate_blocks_the_period(site):
    api, state = site
    _seed(state)
    state.tables["Currency Exchange"] = []

    row = _row(api, state)

    assert row["readiness"] == "blocked"
    assert "exchange_rate_missing" in row["readiness_reasons"]
    assert row["invoice_amount"] is None


def test_a_missing_exchange_rate_is_not_reported_as_unattributed(site):
    api, state = site
    _seed(state)
    state.tables["Currency Exchange"] = []

    row = _row(api, state)

    assert row["readiness"] == "blocked"
    assert "exchange_rate_missing" in row["readiness_reasons"]
    assert "spend_not_attributable" not in row["readiness_reasons"]


def test_an_undecided_tax_treatment_holds_the_draft(site):
    api, state = site
    _seed(state, tax_treatment="")

    row = _row(api, state)

    assert row["readiness"] == "needs_approval"
    assert "tax_treatment_not_decided" in row["readiness_reasons"]


def test_a_tax_template_that_was_not_named_holds_the_draft(site):
    api, state = site
    _seed(state, tax_treatment="Template", tax_template="")

    row = _row(api, state)

    assert row["readiness"] == "needs_approval"
    assert "tax_template_missing" in row["readiness_reasons"]


# ─── THE PREVIEW ───────────────────────────────────────────────────────────────


def test_the_preview_carries_the_lines_the_totals_and_the_sources(site):
    api, state = site
    _seed(state)

    preview = api.preview_invoice(
        COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
    )

    assert len(preview["lines"]) == 1
    line = preview["lines"][0]
    assert line["account"] == ACCOUNT
    assert line["basis_currency"] == "EUR"
    assert line["basis_amount"] == "120.00"
    assert line["fee_percent"] == "12.5"
    assert line["fee_amount"] == "15.00"
    assert line["exchange_rate"] == "1.1"
    assert line["exchange_rate_date"] == "2026-10-31"
    assert line["exchange_rate_source"]
    assert line["charge_amount"] == "148.50"
    assert line["source_sync"] == RUN
    assert line["ad_ids"] == ["ad-1"]
    assert preview["totals"] == {"basis": "120.00", "fee": "15.00", "charge": "148.50"}
    assert preview["readiness"] == "ready"


# ─── THE SITE FACTS THE DRAFT DEPENDS ON ───────────────────────────────────────
#
# ─── WHY THESE TESTS EXIST ─────────────────────────────────────────────────────
#
# Every one of them guards a fact a LIVE ERPNext 16 decided, and every one of
# them guards a defect a mock could not see:
#
#   * ERPNext OVERWRITES a new document's posting date with today unless the
#     document says `set_posting_time` — so an invoice for last month lands in
#     this month's books, silently;
#   * ERPNext's rate field is `conversion_rate`, and it is the invoice's OWN
#     currency into the company's — not the line's rate;
#   * ERPNext refuses an invoice whose RECEIVABLE ACCOUNT is in another
#     currency, and refuses one that cannot be translated into the company's
#     books at all.


def test_the_draft_is_dated_to_the_period_it_bills(site):
    """ERPNext overwrites the posting date with today unless the document says not to.

    An invoice for a period that ended last month would otherwise be posted
    in this month — in the wrong accounting period, and without a word.
    """
    api, state = site
    _seed(state)

    api.create_invoice_draft(
        COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
    )

    invoice = (state.created.get("Sales Invoice") or [])[0]
    assert invoice["posting_date"] == PERIOD_END
    # The flag is ERPNext's own mechanism for a document that carries its own
    # date. Without it the date above is discarded before it is saved.
    assert invoice["set_posting_time"] == 1
    assert invoice["posting_time"] == "00:00:00"


def test_the_draft_carries_the_rate_erpnext_will_translate_it_with(site):
    """The invoice's field is `conversion_rate`, and it is the invoice's own rate.

    This module once wrote `exchange_rate` — which is not a field on an
    ERPNext 16 Sales Invoice at all. The write was silent, and the invoice
    carried no rate.
    """
    api, state = site
    _seed(state)

    api.create_invoice_draft(
        COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
    )

    invoice = (state.created.get("Sales Invoice") or [])[0]
    # USD into the EUR books: 0.9, the site's own approved selling rate.
    assert invoice["conversion_rate"] == 0.9
    assert "exchange_rate" not in invoice


def test_the_draft_names_the_receivable_it_was_checked_against(site):
    api, state = site
    _seed(state)

    api.create_invoice_draft(
        COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
    )

    invoice = (state.created.get("Sales Invoice") or [])[0]
    assert invoice["debit_to"] == RECEIVABLE


def test_a_receivable_in_another_currency_blocks_the_period(site):
    """ERPNext refuses such an invoice, so the app refuses it FIRST.

    "Party Account Debtors - Example currency (EUR) and document currency
    (USD) should be same" is a raw ERPNext error, thrown after the reader has
    pressed a button that looked like it would work.
    """
    api, state = site
    _seed(state)
    state.tables["Account"] = [
        Row(name=RECEIVABLE, account_currency="EUR"),
        Row(name="Sales - Example", account_currency="EUR"),
    ]

    row = _row(api, state)

    assert row["readiness"] == "blocked"
    assert "receivable_account_currency_mismatch" in row["readiness_reasons"]

    with pytest.raises(ValueError, match="not ready to invoice"):
        api.create_invoice_draft(
            COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
        )
    assert state.created == {}


def test_a_customer_without_a_receivable_account_blocks_the_period(site):
    api, state = site
    _seed(state)
    state.tables["Company"] = [
        Row(name=COMPANY, default_currency="EUR", default_receivable_account="")
    ]

    row = _row(api, state)

    assert row["readiness"] == "blocked"
    assert "receivable_account_missing" in row["readiness_reasons"]


def test_a_customer_who_already_trades_in_two_currencies_is_not_blocked(site):
    """ERPNext's own escape hatch: the party already has a posted ledger entry.

    Such a customer is known to carry more than one currency, so one
    receivable account serves both, and ERPNext lets the invoice through.
    """
    api, state = site
    _seed(state)
    state.tables["Account"] = [
        Row(name=RECEIVABLE, account_currency="EUR"),
        Row(name="Sales - Example", account_currency="EUR"),
    ]
    state.tables["GL Entry"] = [
        Row(
            name="gl-1",
            party_type="Customer",
            party=CUSTOMER,
            company=COMPANY,
            docstatus=1,
            is_cancelled=0,
            account_currency="EUR",
        )
    ]

    row = _row(api, state)

    assert "receivable_account_currency_mismatch" not in row["readiness_reasons"]
    assert row["readiness"] == "ready"


def test_a_site_that_allows_one_account_to_carry_many_currencies_is_not_blocked(site):
    api, state = site
    _seed(state)
    state.tables["Account"] = [
        Row(name=RECEIVABLE, account_currency="EUR"),
        Row(name="Sales - Example", account_currency="EUR"),
    ]
    state.settings["Accounts Settings"][
        "allow_multi_currency_invoices_against_single_party_account"
    ] = 1

    row = _row(api, state)

    assert "receivable_account_currency_mismatch" not in row["readiness_reasons"]


def test_an_invoice_that_cannot_be_translated_is_blocked(site):
    """No approved rate for the invoice's own currency into the company's books.

    The charge can be priced and the invoice can still be untranslatable, and
    ERPNext would refuse it at the moment it was created.
    """
    api, state = site
    _seed(state)
    state.tables["Currency Exchange"] = [
        Row(
            name="fx-1",
            from_currency="EUR",
            to_currency="USD",
            date="2026-10-31",
            exchange_rate="1.1",
            for_selling=1,
        )
    ]

    row = _row(api, state)

    assert row["readiness"] == "blocked"
    assert "exchange_rate_missing" in row["readiness_reasons"]

    with pytest.raises(ValueError, match="not ready to invoice"):
        api.create_invoice_draft(
            COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
        )
    assert state.created == {}


def test_a_stale_rate_does_not_count_as_a_rate(site):
    """ERPNext's own staleness rule: a rate older than the window is not a rate."""
    api, state = site
    _seed(state)
    state.tables["Currency Exchange"] = [
        Row(
            name="fx-1",
            from_currency="EUR",
            to_currency="USD",
            date="2026-10-31",
            exchange_rate="1.1",
            for_selling=1,
        ),
        Row(
            name="fx-2",
            from_currency="USD",
            to_currency="EUR",
            date="2026-10-01",
            exchange_rate="0.9",
            for_selling=1,
        ),
    ]

    row = _row(api, state)

    assert row["readiness"] == "blocked"
    assert "exchange_rate_missing" in row["readiness_reasons"]


def test_the_workspace_and_the_preview_agree_on_the_readiness(site):
    """ONE rule. The two readers may not disagree about what may be invoiced.

    The workspace row and the preview used to work the reasons out
    separately, and two copies of one rule can disagree: a period the
    workspace called READY could be refused by the preview, and a reader
    would have had no way of knowing which was right.
    """
    api, state = site
    _seed(state)
    state.tables["Account"] = [
        Row(name=RECEIVABLE, account_currency="EUR"),
        Row(name="Sales - Example", account_currency="EUR"),
    ]

    row = _row(api, state)
    preview = api.preview_invoice(
        COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
    )

    assert row["readiness"] == preview["readiness"]
    assert row["readiness_reasons"] == preview["readiness_reasons"]


# ─── THE WRITE ─────────────────────────────────────────────────────────────────


def test_a_period_that_is_not_ready_is_refused(site):
    api, state = site
    _seed(state, charge_model="")

    with pytest.raises(ValueError, match="not ready to invoice"):
        api.create_invoice_draft(
            COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
        )

    assert state.created == {}


def test_a_ready_period_creates_a_draft_a_batch_and_its_lines(site):
    api, state = site
    _seed(state)

    result = api.create_invoice_draft(
        COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
    )

    invoices = state.created.get("Sales Invoice") or []
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice["customer"] == CUSTOMER
    assert invoice["currency"] == "USD"
    assert invoice["items"][0]["item_code"] == "Meta Ads Recharge"
    assert invoice["items"][0]["rate"] == "148.50"
    assert invoice["items"][0]["income_account"] == "Sales - Example"
    # DRAFT. Not submitted, not posted.
    assert invoice.get("docstatus", 0) == 0
    assert state.submitted == []

    batches = state.created.get("LaraTik Meta Ads Invoice Batch") or []
    assert len(batches) == 1
    batch = batches[0]
    assert batch["billing_customer"] == CUSTOMER
    assert batch["period_start"] == PERIOD_START
    assert batch["period_end"] == PERIOD_END
    assert batch["status"] == "Draft"
    assert batch["sales_invoice"] == invoice["name"]
    assert batch["total"] == "148.50"
    assert batch["charge_model_snapshot"] == "Recharge"
    assert batch["income_account_snapshot"] == "Sales - Example"
    assert batch["source_hash"]

    lines = state.created.get("LaraTik Meta Ads Invoice Line") or []
    assert len(lines) == 1
    assert lines[0]["invoice_batch"] == batch["name"]
    assert lines[0]["basis_amount"] == "120.00"
    assert lines[0]["fee_amount"] == "15.00"
    assert lines[0]["charge_amount"] == "148.50"
    assert lines[0]["exchange_rate"] == "1.1"

    assert result["sales_invoice"] == invoice["name"]
    assert result["status"] == "Draft"


def test_a_period_that_was_already_invoiced_is_never_invoiced_again(site):
    api, state = site
    _seed(state)
    state.tables["LaraTik Meta Ads Invoice Batch"] = [
        Row(
            name="batch-existing",
            company=COMPANY,
            billing_profile=PROFILE,
            billing_customer=CUSTOMER,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            invoice_currency="USD",
            status="Draft",
            sales_invoice="SINV-0001",
            total="148.50",
            basis_total="120.00",
            fee_total="15.00",
            source_hash="abc",
            status_synced_at="2026-11-01 09:00:00",
        ),
    ]

    with pytest.raises(ValueError, match="not ready to invoice"):
        api.create_invoice_draft(
            COMPANY, PERIOD_START, PERIOD_END, PROFILE, CUSTOMER, "12.5", "USD"
        )

    assert state.created == {}


def test_a_period_that_overlaps_an_invoice_is_refused_rather_than_merged(site):
    api, state = site
    _seed(state)
    state.tables["LaraTik Meta Ads Invoice Batch"] = [
        Row(
            name="batch-partial",
            company=COMPANY,
            billing_profile=PROFILE,
            billing_customer=CUSTOMER,
            period_start="2026-10-15",
            period_end="2026-10-31",
            invoice_currency="USD",
            status="Submitted",
            sales_invoice="SINV-0002",
            total="148.50",
            basis_total="120.00",
            fee_total="15.00",
            source_hash="abc",
            status_synced_at="2026-11-01 09:00:00",
        ),
    ]

    row = _row(api, state)

    assert row["readiness"] == "blocked"
    assert "period_overlaps_an_invoice" in row["readiness_reasons"]


def test_the_workspace_reads_the_erpnext_status_back(site):
    """A person may submit the draft in ERPNext; the workspace must show it."""
    api, state = site
    _seed(state)
    state.tables["LaraTik Meta Ads Invoice Batch"] = [
        Row(
            name="batch-existing",
            company=COMPANY,
            billing_profile=PROFILE,
            billing_customer=CUSTOMER,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            invoice_currency="USD",
            status="Draft",
            sales_invoice="SINV-0003",
            total="148.50",
            basis_total="120.00",
            fee_total="15.00",
            source_hash="abc",
            status_synced_at="2026-11-01 09:00:00",
        ),
    ]
    state.tables["Sales Invoice"] = [Row(name="SINV-0003", docstatus=1)]

    row = _row(api, state)

    assert row["readiness"] == "already_invoiced"
    assert row["sales_invoice"] == "SINV-0003"
    assert row["sales_invoice_status"] == "Submitted"
    # What was invoiced before, in the currency it was billed in.
    assert row["previously_invoiced"] == {"USD": "148.50"}
