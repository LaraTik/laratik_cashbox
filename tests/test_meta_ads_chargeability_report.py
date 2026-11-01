"""The SWITCHED-OFF bucket, and the six states on the reports.

The brief is explicit: "Do not treat all unassigned spend as financially
equivalent to intentionally non-billable spend."

Before, a payer arrangement that had been SWITCHED OFF had its Pages dropped
from the asset list before attribution, so its spend came back as UNASSIGNED —
the same bucket, and the same number, as a Page nobody had ever mapped. One
is a decision somebody made and the other is a gap nobody has filled.

These tests hold the separation:

1. The spend of a switched-off arrangement is reported in ITS OWN bucket, and
   it LEAVES the unassigned bucket.
2. The three buckets still ADD UP to the account-level spend. A split that
   does not add up is a split nobody can bill from.
3. A Page nobody has mapped stays unassigned — the separation must not invent
   a decision.
4. The Page report names the state, the reason, and — for a switched-off
   arrangement — the CUSTOMER, so the reader can see whose spend it is and
   why it is not being charged.
5. A conflict is never resolved by picking one of the candidates.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import date, datetime

import pytest


MODULE = "laratik_cashbox.meta_ads_api"

COMPANY = "Example GmbH"
ACCOUNT = "account-1"
RUN = "run-1"
CUSTOMER = "Example Customer"
DISABLED_CUSTOMER = "Disabled Customer"
DISABLED_PROFILE = "Disabled profile"
LIVE_PROFILE = "Live profile"

LIVE_PAGE = "900000000000001"
DISABLED_PAGE = "900000000000002"
UNMAPPED_PAGE = "900000000000003"


# ─── THE FAKE SITE ─────────────────────────────────────────────────────────────


class Row(dict):
    """A row like Frappe's `_dict`: a MAPPING that also answers attributes.

    The report code both reads rows as attributes (`row.amount`) and spreads
    them as mappings (`{**row}`), so the fake has to be both. A
    `SimpleNamespace` is attribute-only and a plain `dict` is mapping-only,
    and either one makes the fake lie about the site it stands in for.
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


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


class FakeSite:
    """Just enough of a site to read the two reports."""

    def __init__(self):
        self.tables = {
            "LaraTik Meta Ads Account": [],
            "LaraTik Meta Ads Sync": [],
            "LaraTik Meta Ads Spend": [],
            "LaraTik Meta Ads Billing Profile": [],
            "LaraTik Meta Ads Page Asset": [],
            "Customer": [],
            "Currency Exchange": [],
        }
        self.queries = []

    def rows(self, doctype, filters=None, limit=None):
        self.queries.append((doctype, filters))
        found = [row for row in self.tables.get(doctype, []) if _matches(row, filters)]
        if limit is not None:
            found = found[:limit]
        return found


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
    fake.get_doc = lambda doctype, name: Row(
        name=name, company=COMPANY, check_permission=lambda permission: None
    )

    state = FakeSite()

    def get_all(doctype, filters=None, fields=None, order_by=None, limit_page_length=None):
        return state.rows(doctype, filters, limit_page_length)

    def get_value(doctype, name, field):
        if doctype == "Currency" and field == "fraction_units":
            return {"EUR": 100, "USD": 100}.get(name)
        if doctype == "Currency Exchange":
            return None
        for row in state.tables.get(doctype, []):
            if getattr(row, "name", None) == name:
                return getattr(row, field, None)
        return None

    fake.db = Row(get_value=get_value, exists=lambda doctype, name: True)
    fake.get_all = get_all
    fake.utils = types.ModuleType("frappe.utils")

    def parse_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

    fake.utils.getdate = parse_date
    fake.utils.now_datetime = lambda: None
    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = RuntimeError
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", fake.utils)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    sys.modules.pop(MODULE, None)
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
        billing_profile=LIVE_PROFILE,
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
        ad_row_count=3,
        ad_reconciliation_status="Matched",
        ad_reconciliation_difference="0.00",
        ad_message="",
        status="Complete",
        from_date="2026-10-01",
        to_date="2026-10-31",
        company=COMPANY,
    )


def _account_spend(spend_date, amount):
    return Row(
        name=f"daily-{spend_date}",
        level="Account",
        sync_run=RUN,
        ad_account=ACCOUNT,
        external_account_id="act_123",
        billing_profile=LIVE_PROFILE,
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


def _ad_spend(ad_id, status, amount, page_id, profile=LIVE_PROFILE, customer=CUSTOMER):
    snapshot = {
        "page_id": page_id,
        "page_name": f"Page {page_id}",
        "instagram_account_id": f"ig-{page_id}",
        "instagram_username": f"user{page_id}",
        "served_customer": "",
    }
    return Row(
        name=f"ad-{ad_id}",
        level="Ad",
        sync_run=RUN,
        ad_account=ACCOUNT,
        external_account_id="act_123",
        billing_profile=profile,
        billing_customer_snapshot=customer,
        relationship_kind_snapshot="Customer",
        fee_percent_snapshot="12.5",
        fee_status_snapshot="configured",
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
        spend_date="2026-10-01",
        amount=amount,
        currency="EUR",
        attribution_status=status,
        page_id=page_id,
        page_name=f"Page {page_id}",
        instagram_actor_id=f"ig-{page_id}",
        company=COMPANY,
    )


def _profile(name, customer, disabled):
    # The POLICY fields are part of the profile now. This fixture declares the
    # Recharge model, because that is the model under which the report's
    # figures were computed before the model existed — the test states the
    # policy, so the application never has to guess it.
    return Row(
        name=name,
        customer=customer,
        company=COMPANY,
        disabled=1 if disabled else 0,
        fee_is_configured=1,
        charge_model="Recharge",
        recognition_model="Principal",
        income_account="",
        invoice_item="",
        tax_treatment="",
        tax_template="",
    )


def _asset(parent, page_id, name="Page"):
    return Row(
        parent=parent,
        parenttype="LaraTik Meta Ads Billing Profile",
        page_id=page_id,
        page_name=name,
        instagram_account_id=f"ig-{page_id}",
        instagram_username=f"user{page_id}",
        served_customer="",
    )


def _customer(name, customer_name):
    return Row(name=name, customer_name=customer_name)


def _seed(state, disabled_profile=True):
    state.tables["LaraTik Meta Ads Account"] = [_account()]
    state.tables["LaraTik Meta Ads Sync"] = [_run()]
    state.tables["LaraTik Meta Ads Spend"] = [
        _account_spend("2026-10-01", "100.00"),
        _account_spend("2026-10-02", "100.00"),
        _ad_spend("ad-1", "Attributed", "120.00", LIVE_PAGE),
        _ad_spend("ad-2", "Unmapped", "50.00", DISABLED_PAGE),
        _ad_spend("ad-3", "Unmapped", "30.00", UNMAPPED_PAGE),
    ]
    state.tables["LaraTik Meta Ads Billing Profile"] = [_profile(LIVE_PROFILE, CUSTOMER, False)]
    if disabled_profile:
        state.tables["LaraTik Meta Ads Billing Profile"].append(
            _profile(DISABLED_PROFILE, DISABLED_CUSTOMER, True)
        )
        state.tables["LaraTik Meta Ads Page Asset"] = [
            _asset(DISABLED_PROFILE, DISABLED_PAGE),
        ]
    state.tables["Customer"] = [
        _customer(CUSTOMER, "Example Customer Name"),
        _customer(DISABLED_CUSTOMER, "Disabled Customer Name"),
    ]
    state.tables["Currency Exchange"] = [
        Row(
            name="fx-1",
            from_currency="EUR",
            to_currency="USD",
            date="2026-10-02",
            exchange_rate="1.1",
            for_selling=1,
        ),
    ]


# ─── THE SPEND REPORT ──────────────────────────────────────────────────────────


def test_the_switched_off_spend_leaves_the_unassigned_bucket(site):
    api, state = site
    _seed(state)

    report = api.get_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    assert report["billing_ready"] is True
    # €200.00 of account spend, in three buckets that add up.
    assert report["spend_by_currency"]["EUR"] == "200.00"
    assert report["not_billable_spend"]["EUR"] == "50.00"
    # €120.00 is billable, €50.00 is switched off, so €30.00 is unassigned —
    # and NOT €80.00, which is what the old reading reported.
    assert report["unassigned_spend"]["EUR"] == "30.00"


def test_the_three_buckets_add_up_to_the_account_spend(site):
    api, state = site
    _seed(state)

    report = api.get_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    billable = sum(
        float(amount)
        for group in report["billing_groups"]
        for amount in group["source_spend"].values()
    )
    not_billable = sum(float(amount) for amount in report["not_billable_spend"].values())
    unassigned = sum(float(amount) for amount in report["unassigned_spend"].values())
    total = sum(float(amount) for amount in report["spend_by_currency"].values())

    assert billable + not_billable + unassigned == total


def test_the_billable_group_is_named_as_billable(site):
    api, state = site
    _seed(state)

    report = api.get_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    assert len(report["billing_groups"]) == 1
    group = report["billing_groups"][0]
    assert group["chargeability"] == "assigned_billable"
    assert group["chargeability_reason"] == "matched_page_and_profile"
    assert group["source_spend"]["EUR"] == "120.00"


def test_a_group_without_an_exchange_rate_is_blocked_and_says_why(site):
    api, state = site
    _seed(state)
    state.tables["Currency Exchange"] = []

    report = api.get_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    group = report["billing_groups"][0]
    assert group["chargeability"] == "blocked"
    assert group["chargeability_reason"] == "exchange_rate_missing"
    # And the amount is withheld, because a missing rate is never zero.
    assert group["invoice_amount"] is None


def test_a_group_with_no_billing_currency_is_blocked(site):
    api, state = site
    _seed(state)
    for row in state.tables["LaraTik Meta Ads Spend"]:
        if row.level == "Ad":
            row.invoice_currency_snapshot = ""

    report = api.get_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    group = report["billing_groups"][0]
    assert group["chargeability"] == "blocked"
    assert group["chargeability_reason"] == "billing_currency_missing"


def test_the_separation_is_not_invented_when_the_arrangement_is_not_switched_off(site):
    # THE CONTROL. With no switched-off arrangement, the same Page stays
    # unassigned — the rule must not manufacture a decision that nobody made.
    api, state = site
    _seed(state, disabled_profile=False)

    report = api.get_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    assert report["not_billable_spend"] == {}
    assert report["unassigned_spend"]["EUR"] == "80.00"


def test_the_switched_off_read_is_three_queries_and_not_two_per_profile(site):
    # A report that reads a hundred disabled arrangements must not issue two
    # hundred queries to say what is switched off.
    api, state = site
    _seed(state)

    api.get_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    queries = [query for query in state.queries if query[0] == "LaraTik Meta Ads Page Asset"]
    assert len(queries) == 1, f"the Page assets were fetched {len(queries)} times"


# ─── THE PAGE REPORT ───────────────────────────────────────────────────────────


def test_the_page_report_names_the_switched_off_page_and_its_customer(site):
    api, state = site
    _seed(state)

    report = api.get_page_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    pages = {page["page_id"]: page for page in report["pages"]}

    assert pages[LIVE_PAGE]["chargeability"] == "assigned_billable"
    assert pages[DISABLED_PAGE]["chargeability"] == "assigned_not_billable"
    assert pages[DISABLED_PAGE]["chargeability_reason"] == "profile_switched_off"
    # The reader can see whose spend it is and why it is not being charged.
    assert pages[DISABLED_PAGE]["chargeability_customer"] == "Disabled Customer Name"
    assert pages[DISABLED_PAGE]["chargeability_profile"] == DISABLED_PROFILE
    # And a Page nobody has mapped is still simply unassigned.
    assert pages[UNMAPPED_PAGE]["chargeability"] == "unassigned"
    assert pages[UNMAPPED_PAGE]["chargeability_reason"] == "no_page_mapping"


def test_a_campaign_inherits_the_state_of_the_page_it_sits_under(site):
    api, state = site
    _seed(state)

    report = api.get_page_spend_report(COMPANY, "2026-10-01", "2026-10-02")

    pages = {page["key"]: page for page in report["pages"]}
    for campaign in report["campaigns"]:
        assert campaign["chargeability"] == pages[campaign["page_key"]]["chargeability"]
        assert campaign["chargeability_reason"] == pages[campaign["page_key"]]["chargeability_reason"]


def test_a_never_resolved_conflict_is_never_resolved_by_the_matcher(site):
    # Two saved assets claiming the same Page is a decision a human has to
    # make. The matcher will not make it by picking one.
    api, _state = site
    conflicting = [
        {"page_id": "900000000000001", "instagram_account_id": "ig-a"},
        {"page_id": "900000000000001", "instagram_account_id": "ig-b"},
    ]

    assert api._match_saved_page({"page_id": "900000000000001"}, conflicting) is None


def test_a_page_and_an_instagram_that_name_different_assets_do_not_match(site):
    api, _state = site
    assets = [
        {"page_id": "900000000000001", "instagram_account_id": "ig-a"},
        {"page_id": "900000000000002", "instagram_account_id": "ig-b"},
    ]

    assert api._match_saved_page(
        {"page_id": "900000000000001", "instagram_actor_id": "ig-b"}, assets
    ) is None


# ─── TRACK B: THE ATTRIBUTION RECONCILIATION ───────────────────────────────────


def test_the_six_states_partition_the_ad_spend(site):
    # The reconciliation's claim: every euro of ad-level delivery spend lands
    # in exactly one of the six states, and the difference between the two
    # sides is the money that did not.
    api, state = site
    _seed(state)

    report = api.get_page_spend_report(COMPANY, "2026-10-01", "2026-10-02")
    reconciliation = report["attribution_reconciliation"]

    assert reconciliation["status"] == "Reconciled"
    assert reconciliation["ad_spend"] == {"EUR": "200.00"}
    assert reconciliation["difference"] == {}
    assert reconciliation["buckets"]["assigned_billable"] == {"EUR": "120.00"}
    assert reconciliation["buckets"]["assigned_not_billable"] == {"EUR": "50.00"}
    assert reconciliation["buckets"]["unassigned"] == {"EUR": "30.00"}
    assert reconciliation["buckets"]["ambiguous"] == {}

    placed = sum(
        float(amount)
        for bucket in reconciliation["buckets"].values()
        for amount in bucket.values()
    )
    assert placed == float(reconciliation["ad_spend"]["EUR"])


def test_a_row_that_cannot_be_read_is_INCOMPLETE_and_not_reconciled(site):
    # An unreadable row has an UNKNOWN amount, so it moves neither side of
    # the comparison and the difference stays zero. Calling that "reconciled"
    # would be a figure the reader could trust too far.
    api, state = site
    _seed(state)
    state.tables["LaraTik Meta Ads Spend"].append(
        _ad_spend("ad-bad", "Unmapped", "not-a-number", "900000000000009")
    )

    report = api.get_page_spend_report(COMPANY, "2026-10-01", "2026-10-02")
    reconciliation = report["attribution_reconciliation"]

    assert reconciliation["status"] == "Incomplete"
    assert reconciliation["unreadable_rows"] >= 1


def test_the_reconciliation_reports_a_difference_when_the_buckets_do_not_add_up(site):
    # The self-check, exercised directly: a bucket that does not hold the
    # money it claims must be reported, not smoothed away.
    api, _state = site
    from collections import defaultdict
    from decimal import Decimal

    buckets = {
        "assigned_billable": defaultdict(Decimal),
        "assigned_not_billable": defaultdict(Decimal),
        "unassigned": defaultdict(Decimal),
        "ambiguous": defaultdict(Decimal),
        "already_invoiced": defaultdict(Decimal),
        "blocked": defaultdict(Decimal),
    }
    buckets["assigned_billable"]["EUR"] = Decimal("10.00")

    reconciliation = api._attribution_reconciliation(
        buckets, {"EUR": Decimal("12.00")}, 0, False, True
    )

    assert reconciliation["status"] == "Difference"
    assert reconciliation["difference"] == {"EUR": "2.00"}
