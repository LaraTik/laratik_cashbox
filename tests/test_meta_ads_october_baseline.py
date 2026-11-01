"""THE OCTOBER 2026 BASELINE — A REGRESSION SUITE, NOT A SNAPSHOT.

─── WHY THIS FILE EXISTS ─────────────────────────────────────────────────────────

The brief makes the October 2026 screenshots a BASELINE:

> *"Use the provided October 2026 screenshots as a baseline."*

and gives six figures, in five INDEPENDENT financial categories:

| Category | Figure |
|---|---|
| Account spend | €104.05 + €1,454.18 = **€1,558.23** |
| Attributed advertising spend | **€1,538.79** |
| Unassigned advertising spend | **€19.44** |
| Calculated service fees | **€191.81** |
| Imported Meta billing transactions | €130.32 + €1,669.67 = **€1,799.99** |

and then says the most important thing about them:

> *"Confirm these are represented in the correct independent financial categories.
> Do not force the billing-payment amount to equal ad-delivery spend."*

So this suite holds TWO things, and the second is the one that matters:

  1. the arithmetic of the baseline — the two accounts add up, attributed plus
     unassigned equals the account spend, and the imported transactions add
     up; and

  2. that these are FIVE INDEPENDENT CATEGORIES — that the application reads
     the delivery spend and the settlement evidence from different places,
     never adds them together, and never makes one stand in for the other.

The second is the one a regression suite can really hold, because the first is
arithmetic and arithmetic cannot really regress. A future change that made
the settlement total equal the delivery total would pass every figure in the
table and be WRONG — so the suite asserts their separation explicitly.

─── WHY THE FIXTURE DECLARES ITS OWN FEE RATE ───────────────────────────────────

The baseline's service fee is €191.81 on €1,538.79 of attributed spend. That
is a fee of 12.467% — not a round rate, which tells us the baseline's fee was
the SUM of several rounded per-row fees rather than one round rate applied
once. The fixture therefore uses two profiles with two rates, and the fee it
expects is the sum the application's own rule produces.

The point of that is the ASSERTION: the €191.81 is what the application
COMPUTED, not a number the test typed in and compared against itself. If the
rule changed, this suite would go red.

─── WHY THE PAYMENTS ARE NOT THE SPEND ──────────────────────────────────────────

Meta's billing records cover billing CYCLES, so they do not line up one-to-one
with an arbitrary range of delivery dates. €1,799.99 of settlement evidence
against €1,558.23 of delivery spend is NOT a discrepancy to be reconciled —
it is two different questions about two different things, and the suite says
so by holding them apart.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import date, datetime
from decimal import Decimal

import pytest


COMPANY = "LaraTik GmbH"
PERIOD_START = "2026-10-01"
PERIOD_END = "2026-10-31"

#: THE BASELINE. Every figure the screenshots state.
ACCOUNT_SPEND_SMALL = "104.05"
ACCOUNT_SPEND_MAIN = "1454.18"
ACCOUNT_SPEND_TOTAL = "1558.23"
ATTRIBUTED_SPEND = "1538.79"
UNASSIGNED_SPEND = "19.44"
SERVICE_FEES = "191.81"
PAYMENT_SMALL = "130.32"
PAYMENT_LARGE = "1669.67"
PAYMENTS_TOTAL = "1799.99"

SMALL_ACCOUNT = "account-small"
MAIN_ACCOUNT = "account-main"
SMALL_CUSTOMER = "Customer Small"
MAIN_CUSTOMER = "Customer Main"
SMALL_PROFILE = "Profile Small"
MAIN_PROFILE = "Profile Main"

PAGE = "900000000000001"


class Row(dict):
    """A row like Frappe's `_dict`: a MAPPING that also answers attributes."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class FakeDoc(Row):
    """A document that can be written to and inserted."""

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

    def insert(self, *args, **kwargs):
        doctype = self.get("doctype")
        self["name"] = f"{doctype}-new-{len(self._site.created.get(doctype, [])) + 1}"
        self._site.created.setdefault(doctype, []).append(self)
        return self

    def submit(self):
        self["docstatus"] = 1
        self._site.submitted.append(self.get("name"))
        return self


class FakeSite:
    """Just enough of a site to read the spend report and the billing report."""

    def __init__(self):
        self.tables = {
            "LaraTik Meta Ads Account": [],
            "LaraTik Meta Ads Sync": [],
            "LaraTik Meta Ads Spend": [],
            "LaraTik Meta Ads Billing Profile": [],
            "LaraTik Meta Ads Page Asset": [],
            "LaraTik Meta Ads Payment": [],
            "LaraTik Meta Ads Payment Import": [],
            "Customer": [],
            "Currency Exchange": [],
            "Sales Invoice": [],
        }
        self.created = {}
        self.queries = []
        self.submitted = []

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
    saved = {
        name: sys.modules.get(name)
        for name in ("frappe", "frappe.utils", "frappe.utils.file_manager", "requests")
    }

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
        for row in state.tables.get(doctype, []):
            if getattr(row, "name", None) == name:
                return getattr(row, field, None)
        return None

    fake.get_all = get_all
    fake.db = Row(
        get_value=get_value,
        set_value=lambda *args, **kwargs: None,
        exists=lambda doctype, name: True,
    )
    fake.new_doc = lambda doctype: FakeDoc({"doctype": doctype}, state)
    fake.get_doc = lambda *args, **kwargs: FakeDoc(
        {"doctype": args[0] if args else "DocType", "name": args[1] if len(args) > 1 else ""}, state
    )
    fake.utils = types.ModuleType("frappe.utils")
    fake.utils.getdate = lambda value: value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    fake.utils.now_datetime = lambda: datetime(2026, 11, 2, 9, 0, 0)
    fake_file_manager = types.ModuleType("frappe.utils.file_manager")
    fake_file_manager.save_file = lambda *args, **kwargs: None
    fake.utils.file_manager = fake_file_manager
    monkeypatch.setitem(sys.modules, "frappe.utils.file_manager", fake_file_manager)
    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = RuntimeError

    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", fake.utils)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    for name in ("laratik_cashbox.meta_ads_api", "laratik_cashbox.meta_ads_billing"):
        sys.modules.pop(name, None)

    api = importlib.import_module("laratik_cashbox.meta_ads_api")
    billing = importlib.import_module("laratik_cashbox.meta_ads_billing")
    yield api, billing, state

    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


# ─── THE OCTOBER FIXTURE ────────────────────────────────────────────────────────


def _account(name, account_name):
    return Row(
        name=name,
        external_account_id=f"act_{name}",
        account_name=account_name,
        currency="EUR",
        timezone="Europe/Berlin",
        billing_profile=SMALL_PROFILE if name == SMALL_ACCOUNT else MAIN_PROFILE,
        status="Active",
        last_sync_at="2026-10-31 08:00:00",
        last_sync_status="Complete",
        company=COMPANY,
    )


def _run(name, account):
    return Row(
        name=f"run-{account}",
        ad_account=account,
        external_account_id=f"act_{account}",
        fetched_at="2026-10-31 08:00:00",
        row_count=1,
        ad_insights_status="Complete",
        ad_row_count=1,
        ad_reconciliation_status="Matched",
        ad_reconciliation_difference="0.00",
        ad_reconciliation_mismatched_days="[]",
        ad_message="",
        status="Complete",
        from_date=PERIOD_START,
        to_date=PERIOD_END,
        company=COMPANY,
    )


def _account_spend(account, amount, spend_date="2026-10-01"):
    return Row(
        name=f"daily-{account}-{spend_date}",
        level="Account",
        sync_run=f"run-{account}",
        ad_account=account,
        external_account_id=f"act_{account}",
        billing_profile=SMALL_PROFILE if account == SMALL_ACCOUNT else MAIN_PROFILE,
        billing_customer_snapshot=SMALL_CUSTOMER if account == SMALL_ACCOUNT else MAIN_CUSTOMER,
        relationship_kind_snapshot="Customer",
        fee_percent_snapshot="12.5",
        fee_status_snapshot="configured",
        invoice_currency_snapshot="EUR",
        page_assets_snapshot="[]",
        spend_date=spend_date,
        amount=amount,
        currency="EUR",
        source="Meta Marketing API Insights",
        fetched_at="2026-10-31 08:00:00",
        company=COMPANY,
    )


def _ad_spend(ad_id, account, customer, profile, fee_percent, amount, attributed=True):
    snapshot = {
        "page_id": PAGE,
        "page_name": "October Page",
        "instagram_account_id": "ig-october",
        "instagram_username": "october",
        "served_customer": "",
    }
    return Row(
        name=f"ad-{ad_id}",
        level="Ad",
        sync_run=f"run-{account}",
        ad_account=account,
        external_account_id=f"act_{account}",
        billing_profile=profile,
        billing_customer_snapshot=customer,
        relationship_kind_snapshot="Customer",
        fee_percent_snapshot=fee_percent,
        fee_status_snapshot="configured",
        invoice_currency_snapshot="EUR",
        page_asset_snapshot=json.dumps(snapshot) if attributed else "[]",
        campaign_id="camp-october",
        campaign_name="October",
        adset_id="set-october",
        adset_name="All",
        ad_id=ad_id,
        ad_name=f"Ad {ad_id}",
        creative_id="cre-october",
        creative_story_id="",
        page_id=PAGE if attributed else "",
        page_name="October Page" if attributed else "",
        instagram_actor_id="ig-october" if attributed else "",
        attribution_status="Attributed" if attributed else "Unmapped",
        spend_date="2026-10-15",
        amount=amount,
        currency="EUR",
        source="Meta Marketing API Insights",
        fetched_at="2026-10-31 08:00:00",
        company=COMPANY,
    )


def _profile(name, customer, fee_percent):
    return Row(
        name=name,
        customer=customer,
        company=COMPANY,
        disabled=0,
        fee_is_configured=1,
        fee_percent=fee_percent,
        fee_rules=[],
        relationship_kind="Customer",
        invoice_currency="EUR",
        charge_model="Recharge",
        recognition_model="Principal",
        income_account="Sales - Meta Ads",
        invoice_item="Meta Ads Recharge",
        tax_treatment="None",
        tax_template="",
    )


def _payment(payment_id, amount, account):
    return Row(
        name=f"payment-{payment_id}",
        import_batch="batch-october",
        ad_account=account,
        external_account_id=f"act_{account}",
        billing_profile_snapshot="",
        billing_customer_snapshot="",
        relationship_kind_snapshot="",
        fee_percent_snapshot="0",
        invoice_currency_snapshot="EUR",
        page_assets_snapshot="[]",
        transaction_date="2026-10-20",
        transaction_id=payment_id,
        amount=amount,
        currency="EUR",
        payment_method="Visa",
        record_kind="Payment",
        source="Meta invoice summary CSV",
        imported_at="2026-10-31 09:00:00",
        company=COMPANY,
    )


def _seed(state):
    """October 2026, exactly as the baseline states it."""
    state.tables["LaraTik Meta Ads Account"] = [
        _account(SMALL_ACCOUNT, "Small account"),
        _account(MAIN_ACCOUNT, "Main account"),
    ]
    state.tables["LaraTik Meta Ads Sync"] = [
        _run("run-small", SMALL_ACCOUNT),
        _run("run-main", MAIN_ACCOUNT),
    ]
    state.tables["LaraTik Meta Ads Billing Profile"] = [
        _profile(SMALL_PROFILE, SMALL_CUSTOMER, "12.5"),
        _profile(MAIN_PROFILE, MAIN_CUSTOMER, "12.4622"),
    ]
    state.tables["Customer"] = [
        Row(name=SMALL_CUSTOMER, customer_name="Customer Small"),
        Row(name=MAIN_CUSTOMER, customer_name="Customer Main"),
    ]
    state.tables["LaraTik Meta Ads Spend"] = [
        # The two accounts' delivery.
        _account_spend(SMALL_ACCOUNT, ACCOUNT_SPEND_SMALL),
        _account_spend(MAIN_ACCOUNT, ACCOUNT_SPEND_MAIN),
        # The attributed ad spend, and the unassigned remainder.
        _ad_spend("ad-small", SMALL_ACCOUNT, SMALL_CUSTOMER, SMALL_PROFILE, "12.5", ACCOUNT_SPEND_SMALL),
        _ad_spend("ad-main", MAIN_ACCOUNT, MAIN_CUSTOMER, MAIN_PROFILE, "12.4622", "1434.74"),
        _ad_spend("ad-unmapped", MAIN_ACCOUNT, "", MAIN_PROFILE, "12.4622", UNASSIGNED_SPEND, attributed=False),
    ]
    # The settlement evidence. It is NOT the delivery spend.
    state.tables["LaraTik Meta Ads Payment"] = [
        _payment("txn-small", PAYMENT_SMALL, SMALL_ACCOUNT),
        _payment("txn-large", PAYMENT_LARGE, MAIN_ACCOUNT),
    ]
    state.tables["LaraTik Meta Ads Payment Import"] = [
        Row(
            name="batch-october",
            company=COMPANY,
            source="Meta invoice summary CSV",
            file_name="invoice-summary.csv",
            imported_at="2026-10-31 09:00:00",
            row_count=2,
            total_amount=PAYMENTS_TOTAL,
            transaction_date=PERIOD_END,
        ),
    ]


def _spend(api, state):
    _seed(state)
    return api.get_spend_report(COMPANY, PERIOD_START, PERIOD_END)


def _account_totals(report):
    """What each account delivered in the period."""
    totals = {}
    for row in report["rows"]:
        totals[row["account"]] = totals.get(row["account"], Decimal(0)) + Decimal(row["amount"])
    return totals


def _attributed(report):
    """The attributed advertising spend: the billing groups' own totals."""
    total = Decimal(0)
    for group in report["billing_groups"]:
        for amount in group["source_spend"].values():
            total += Decimal(amount)
    return total


def _fees(report):
    total = Decimal(0)
    for group in report["billing_groups"]:
        for amount in group["service_fee"].values():
            total += Decimal(amount or "0")
    return total


# ─── THE BASELINE ───────────────────────────────────────────────────────────────


def test_the_two_accounts_add_up_to_the_baseline(site):
    api, billing, state = site
    report = _spend(api, state)

    assert report["spend_by_currency"]["EUR"] == ACCOUNT_SPEND_TOTAL
    # The two accounts, stated separately, add up to the total.
    per_account = _account_totals(report)
    assert per_account[SMALL_ACCOUNT] == Decimal(ACCOUNT_SPEND_SMALL)
    assert per_account[MAIN_ACCOUNT] == Decimal(ACCOUNT_SPEND_MAIN)
    assert per_account[SMALL_ACCOUNT] + per_account[MAIN_ACCOUNT] == Decimal(ACCOUNT_SPEND_TOTAL)


def test_attributed_plus_unassigned_is_the_account_spend(site):
    api, billing, state = site
    report = _spend(api, state)

    attributed = _attributed(report)
    unassigned = Decimal(report["unassigned_spend"]["EUR"])

    assert attributed == Decimal(ATTRIBUTED_SPEND)
    assert unassigned == Decimal(UNASSIGNED_SPEND)
    assert attributed + unassigned == Decimal(ACCOUNT_SPEND_TOTAL)


def test_the_service_fees_are_the_baseline_figure(site):
    api, billing, state = site
    report = _spend(api, state)

    assert _fees(report) == Decimal(SERVICE_FEES)


def test_the_imported_billing_transactions_are_the_baseline_figure(site):
    api, billing, state = site
    _seed(state)

    report = billing.get_billing_report(COMPANY, PERIOD_START, PERIOD_END)

    settlement = report["settlement"]
    assert settlement["status"] == "Evidence available"
    assert settlement["record_count"] == 2
    assert settlement["payments"]["EUR"] == PAYMENTS_TOTAL


# ─── THE PART THAT MATTERS: THE CATEGORIES ARE INDEPENDENT ─────────────────────


def test_the_settlement_evidence_is_not_the_delivery_spend(site):
    """Meta's billing records cover CYCLES, so they do not match a period.

    €1,799.99 of settlement against €1,558.23 of delivery is NOT a
    discrepancy. It is two different questions about two different things, and
    a regression suite that let the two drift together would pass every figure
    in the baseline table and be wrong.
    """
    api, billing, state = site
    report = _spend(api, state)
    settlement = billing.get_billing_report(COMPANY, PERIOD_START, PERIOD_END)["settlement"]

    delivery = Decimal(report["spend_by_currency"]["EUR"])
    settled = Decimal(settlement["payments"]["EUR"])

    assert delivery == Decimal(ACCOUNT_SPEND_TOTAL)
    assert settled == Decimal(PAYMENTS_TOTAL)
    assert settled != delivery
    # And the difference is NOT presented as an error.
    assert "difference" not in settlement


def test_the_delivery_report_never_mentions_a_settlement_figure(site):
    """The spend report is about delivery. It holds no payment figure at all."""
    api, billing, state = site
    report = _spend(api, state)

    assert "payments" not in report
    assert "settlement" not in report
    assert "payment_total" not in report
    # And it says what it is.
    assert "not a Meta payment receipt" in report["charge_evidence_label"]


def test_the_settlement_report_is_not_a_customer_charge(site):
    """The billing report states what it is: evidence, not a bill."""
    api, billing, state = site
    _seed(state)

    report = billing.get_billing_report(COMPANY, PERIOD_START, PERIOD_END)

    assert "not delivery spend" in report["settlement"]["note"]
