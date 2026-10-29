"""Ad-level Insights must attribute only to a verified creative Page identity."""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.meta_ads_api"


@pytest.fixture
def meta_api(monkeypatch):
    saved_frappe = sys.modules.get("frappe")
    saved_utils = sys.modules.get("frappe.utils")
    saved_requests = sys.modules.get("requests")
    fake = types.ModuleType("frappe")
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.db = SimpleNamespace(
        get_value=lambda doctype, currency, field: 100 if currency == "EUR" else 1,
        exists=lambda doctype, name: True,
    )
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
    yield api
    sys.modules.pop(MODULE, None)
    if saved_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = saved_frappe
    if saved_utils is None:
        sys.modules.pop("frappe.utils", None)
    else:
        sys.modules["frappe.utils"] = saved_utils
    if saved_requests is None:
        sys.modules.pop("requests", None)
    else:
        sys.modules["requests"] = saved_requests


def assets():
    return [{
        "page_id": "900000000000001",
        "page_name": "Example Page",
        "instagram_account_id": "17841400000000001",
        "instagram_username": "reem",
        "served_customer": "Example Customer",
    }]


def payer_profile(name="Example profile", page_assets_override=None):
    return {
        "name": name,
        "customer": "Example Customer",
        "relationship_kind": "Customer",
        "fee_percent": "0",
        "invoice_currency": "USD",
        "disabled": False,
        "page_assets": page_assets_override if page_assets_override is not None else assets(),
    }


def ad_spend_row(status, amount="10.00", snapshot=None, profile="Example profile", page_id="900000000000001"):
    asset = snapshot if snapshot is not None else {
        "page_id": "900000000000001",
        "page_name": "Example Page",
        "instagram_account_id": "17841400000000001",
        "instagram_username": "reem",
        "served_customer": "",
    }
    return SimpleNamespace(
        level="Ad",
        ad_account="account-1",
        sync_run="run-1",
        external_account_id="act_123",
        amount=amount,
        currency="EUR",
        ad_id="ad-1",
        attribution_status=status,
        page_asset_snapshot=json.dumps(asset) if isinstance(asset, dict) else asset,
        billing_profile=profile,
        billing_customer_snapshot="Example Customer",
        relationship_kind_snapshot="Customer",
        fee_percent_snapshot="0",
        invoice_currency_snapshot="USD",
        page_id=page_id,
        instagram_actor_id="17841400000000001",
    )


def account_and_run():
    account = SimpleNamespace(name="account-1", external_account_id="act_123", account_name="Example Ads", currency="EUR")
    run = SimpleNamespace(name="run-1")
    return {"account-1": account}, {"account-1": run}


def test_ad_with_matching_facebook_and_instagram_identity_is_attributed(meta_api):
    result = meta_api._attribute_ad_page({
        "page_id": "900000000000001",
        "instagram_actor_id": "17841400000000001",
    }, assets())

    assert result["status"] == "Attributed"
    assert result["page_name"] == "Example Page"
    assert result["asset"]["instagram_username"] == "reem"


def test_ad_can_get_page_identity_from_effective_story_id(meta_api):
    result = meta_api._attribute_ad_page({
        "page_id": "",
        "effective_object_story_id": "900000000000001_12345",
        "instagram_actor_id": "17841400000000001",
    }, assets())

    assert result["status"] == "Attributed"
    assert result["page_id"] == "900000000000001"


def test_conflicting_explicit_and_effective_story_page_ids_are_ambiguous(meta_api):
    result = meta_api._attribute_ad_page({
        "page_id": "different-page",
        "effective_object_story_id": "900000000000001_12345",
        "instagram_actor_id": "",
    }, assets())

    assert result["status"] == "Ambiguous"
    assert result["asset"] is None


def test_ad_whose_page_and_instagram_map_to_different_assets_is_ambiguous(meta_api):
    other = {
        "page_id": "other-page",
        "page_name": "Other Page",
        "instagram_account_id": "other-ig",
        "instagram_username": "other",
        "served_customer": "",
    }

    result = meta_api._attribute_ad_page({
        "page_id": "900000000000001",
        "instagram_actor_id": "other-ig",
    }, assets() + [other])

    assert result["status"] == "Ambiguous"
    assert result["asset"] is None


def test_ad_identity_without_a_profile_mapping_stays_unmapped(meta_api):
    result = meta_api._attribute_ad_page({
        "page_id": "unmapped-page",
        "instagram_actor_id": "unmapped-ig",
    }, assets())

    assert result["status"] == "Unmapped"
    assert result["page_id"] == "unmapped-page"
    assert result["asset"] is None


def test_page_asset_mapping_retains_the_payer_profile(meta_api):
    profile = payer_profile()
    mapped_assets = meta_api._page_profile_assets([profile])

    result = meta_api._attribute_ad_page({
        "page_id": "900000000000001",
        "instagram_actor_id": "17841400000000001",
    }, mapped_assets)

    assert result["status"] == "Attributed"
    assert result["asset"]["_billing_profile"] == profile


def test_only_page_matched_ad_cost_is_added_to_customer_charge_group(meta_api, monkeypatch):
    monkeypatch.setattr(meta_api.frappe.db, "get_value", lambda doctype, name, field: name)
    account_by_name, run_by_account = account_and_run()
    rows = [
        ad_spend_row("Attributed", "12.34"),
        ad_spend_row("Unmapped", "87.66", snapshot="", profile=""),
    ]

    groups, unreadable = meta_api._collect_ad_billable_groups(rows, account_by_name, run_by_account)

    assert unreadable == 0
    assert len(groups) == 1
    assert groups["Example profile|Example Customer|0|USD"]["source_spend"] == {"EUR": Decimal("12.34")}


def test_account_level_total_cannot_enter_a_customer_charge_group(meta_api):
    account_by_name, run_by_account = account_and_run()
    row = ad_spend_row("Attributed", "100.00")
    row.level = "Account"

    groups, unreadable = meta_api._collect_ad_billable_groups([row], account_by_name, run_by_account)

    assert groups == {}
    assert unreadable == 1


def test_duplicate_page_assets_are_ambiguous_and_not_billable(meta_api, monkeypatch):
    monkeypatch.setattr(meta_api.frappe.db, "get_value", lambda doctype, name, field: name)
    duplicate_profile = payer_profile("Other profile")
    mapped_assets = meta_api._page_profile_assets([payer_profile(), duplicate_profile])
    attribution = meta_api._attribute_ad_page({
        "page_id": "900000000000001",
        "instagram_actor_id": "17841400000000001",
    }, mapped_assets)
    account_by_name, run_by_account = account_and_run()
    groups, unreadable = meta_api._collect_ad_billable_groups(
        [ad_spend_row(attribution["status"], "99.00", snapshot="", profile="")],
        account_by_name,
        run_by_account,
    )

    assert attribution["status"] == "Ambiguous"
    assert groups == {}
    assert unreadable == 0


def test_attributed_ad_with_malformed_page_snapshot_withholds_billing(meta_api):
    account_by_name, run_by_account = account_and_run()

    groups, unreadable = meta_api._collect_ad_billable_groups(
        [ad_spend_row("Attributed", "10.00", snapshot="not-json")],
        account_by_name,
        run_by_account,
    )

    assert groups == {}
    assert unreadable == 1


def test_daily_ad_spend_is_joined_to_creatives_and_campaign_identity(meta_api, monkeypatch):
    calls = []

    def fake_meta_get(version, path, params, token):
        calls.append((path, params))
        if path.endswith("/insights"):
            return {
                "data": [{
                    "date_start": "2026-10-01",
                    "date_stop": "2026-10-01",
                    "ad_id": "1001",
                    "ad_name": "October ad",
                    "adset_id": "set-1",
                    "adset_name": "Audience",
                    "campaign_id": "campaign-1",
                    "campaign_name": "October campaign",
                    "spend": "12.34",
                }]
            }
        if path.endswith("/ads"):
            return {"data": [{
                "id": "1001",
                "creative": {
                    "id": "2001",
                    "object_story_spec": {},
                    "instagram_actor_id": "ig-1",
                    "effective_object_story_id": "123456789_98765",
                },
            }]}
        raise AssertionError(f"Unexpected Meta API path: {path}")

    monkeypatch.setattr(meta_api, "_meta_get", fake_meta_get)

    rows = meta_api._read_daily_ad_spend(
        "v26.0", "test-token", "act_123", "2026-10-01", "2026-10-31"
    )

    assert len(rows) == 1
    assert rows[0]["amount"] == Decimal("12.34")
    assert rows[0]["campaign_name"] == "October campaign"
    assert rows[0]["creative_id"] == "2001"
    assert rows[0]["creative_story_id"] == "123456789_98765"
    assert rows[0]["page_id"] == "123456789"
    assert rows[0]["instagram_actor_id"] == "ig-1"
    assert [path for path, _ in calls] == ["act_123/insights", "act_123/ads"]
    assert calls[0][1]["level"] == "ad"
    assert calls[0][1]["time_increment"] == 1


def test_ad_spend_must_reconcile_each_date_not_just_period_total(meta_api):
    account_daily = {
        "2026-10-01": Decimal("10.00"),
        "2026-10-02": Decimal("20.00"),
    }
    ad_rows = [
        {"spend_date": "2026-10-01", "amount": Decimal("9.00")},
        {"spend_date": "2026-10-02", "amount": Decimal("21.00")},
    ]

    status, difference, mismatched_days = meta_api._reconcile_ad_spend(account_daily, ad_rows, "EUR")

    assert status == "Mismatch"
    assert difference == "0.00"
    assert mismatched_days == 2


def test_matching_daily_account_and_ad_spend_reconciles(meta_api):
    status, difference, mismatched_days = meta_api._reconcile_ad_spend(
        {"2026-10-01": Decimal("10.00")},
        [{"spend_date": "2026-10-01", "amount": Decimal("10.00")}],
        "EUR",
    )

    assert status == "Matched"
    assert difference == "0.00"
    assert mismatched_days == 0


def test_fee_schedule_uses_the_last_rate_effective_on_or_before_spend_date(meta_api):
    profile = {
        "fee_percent": "15",
        "fee_rules": [
            {"effective_from": "2026-10-04", "fee_percent": "25"},
            {"effective_from": "2026-10-03", "fee_percent": "20"},
        ],
    }

    assert meta_api._fee_percent_for_date(profile, "2026-10-02") == "15"
    assert meta_api._fee_percent_for_date(profile, "2026-10-03") == "20"
    assert meta_api._fee_percent_for_date(profile, "2026-10-04") == "25"


def test_fee_schedule_parser_orders_rules_and_rejects_duplicate_dates(meta_api):
    rules = meta_api._parse_fee_rules([
        {"effective_from": "2026-10-04", "fee_percent": "25"},
        {"effective_from": "2026-10-03", "fee_percent": "20"},
    ])
    assert [row["effective_from"] for row in rules] == ["2026-10-03", "2026-10-04"]

    with pytest.raises(ValueError, match="only one fee rule"):
        meta_api._parse_fee_rules([
            {"effective_from": "2026-10-03", "fee_percent": "20"},
            {"effective_from": "2026-10-03", "fee_percent": "25"},
        ])
