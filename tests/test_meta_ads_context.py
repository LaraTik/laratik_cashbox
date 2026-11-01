"""The workspace context: one cheap, company-scoped read of the scope.

ADR-227. Every section of the Meta Ads Finance workspace states the same
scope — the company, the reporting currency, the connected accounts, and the
last successful synchronisation. Before, each section built that scope from
whichever report it happened to read, so the account selector vanished on the
settlement screen and the last-sync stamp was missing from two of the five
tabs.

The rules this test holds:

1. It is COMPANY-SCOPED. Another company's accounts are never returned.
2. It states the REPORTING CURRENCY — the company's own currency, which is
   what the report is read in. It is NOT the Meta account's currency, which is
   a different thing entirely.
3. It reports the LAST SUCCESSFUL synchronisation. An account whose last sync
   FAILED does not contribute a timestamp, and does not make the workspace
   look fresh.
4. It SAYS SO when the account list is truncated. A silently shortened list
   reads as "these are all the accounts".
5. It refuses a company that does not exist, and it is admin-gated.
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.meta_ads_api"


class FakeCompany:
    def __init__(self, name: str):
        self.name = name

    def check_permission(self, permission: str):
        return None


def _account(name, account_name, currency="EUR", last_sync_at="", last_sync_status=""):
    return SimpleNamespace(
        name=name,
        external_account_id=f"act_{name}",
        account_name=account_name,
        currency=currency,
        timezone="Europe/Berlin",
        status="Active",
        last_sync_at=last_sync_at,
        last_sync_status=last_sync_status,
    )


@pytest.fixture
def meta_api(monkeypatch):
    saved_frappe = sys.modules.get("frappe")
    saved_utils = sys.modules.get("frappe.utils")
    saved_requests = sys.modules.get("requests")
    fake = types.ModuleType("frappe")
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.only_for = lambda role: (_ for _ in ()).throw(PermissionError(role))
    fake.session = SimpleNamespace(user="Administrator")
    fake.get_doc = lambda doctype, name: FakeCompany(name)

    state = {"accounts": [], "currency": "EUR", "exists": True, "queries": []}

    def get_value(doctype, name, field):
        state["queries"].append((doctype, name, field))
        if doctype == "Company" and field == "default_currency":
            return state["currency"]
        return None

    def get_all(doctype, filters=None, fields=None, order_by=None, limit_page_length=None):
        state["queries"].append((doctype, filters, order_by, limit_page_length))
        assert doctype == "LaraTik Meta Ads Account"
        assert filters == {"company": "Example GmbH"}
        rows = state["accounts"]
        if limit_page_length is not None:
            rows = rows[:limit_page_length]
        return rows

    fake.db = SimpleNamespace(get_value=get_value, exists=lambda doctype, name: state["exists"])
    fake.get_all = get_all
    fake.utils = types.ModuleType("frappe.utils")
    fake.utils.getdate = lambda value: value
    fake.utils.now_datetime = lambda: None
    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = RuntimeError
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", fake.utils)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    sys.modules.pop(MODULE, None)
    api = importlib.import_module(MODULE)
    api.TEST_STATE = state
    yield api
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


def test_it_states_the_company_the_reporting_currency_and_the_accounts(meta_api):
    meta_api.TEST_STATE["accounts"] = [
        _account("META-1", "Account One", "EUR"),
        _account("META-2", "Account Two", "USD"),
    ]
    meta_api.TEST_STATE["currency"] = "EUR"

    context = meta_api.get_meta_ads_context("Example GmbH")

    assert context["company"] == "Example GmbH"
    # The REPORTING currency is the company's, not the Meta account's. A Meta
    # account denominated in USD does not make the report USD.
    assert context["reporting_currency"] == "EUR"
    assert [account["name"] for account in context["accounts"]] == ["META-1", "META-2"]
    assert context["accounts"][1]["currency"] == "USD"
    assert context["accounts_truncated"] is False


def test_it_is_scoped_to_the_company(meta_api):
    meta_api.TEST_STATE["accounts"] = [_account("META-1", "Account One")]

    meta_api.get_meta_ads_context("Example GmbH")

    account_queries = [
        query for query in meta_api.TEST_STATE["queries"]
        if query[0] == "LaraTik Meta Ads Account"
    ]
    assert account_queries
    assert account_queries[0][1] == {"company": "Example GmbH"}


def test_it_refuses_a_company_that_does_not_exist(meta_api):
    meta_api.TEST_STATE["exists"] = False

    with pytest.raises(ValueError):
        meta_api.get_meta_ads_context("Nowhere GmbH")


def test_it_reports_the_last_successful_synchronisation_and_ignores_a_failed_one(meta_api):
    meta_api.TEST_STATE["accounts"] = [
        _account("META-1", "Older", "EUR", "2026-10-20 08:00:00", "Complete"),
        _account("META-2", "Newest", "EUR", "2026-10-29 10:00:00", "Complete"),
        _account("META-3", "Failed", "EUR", "2026-10-31 12:00:00", "Failed"),
    ]

    context = meta_api.get_meta_ads_context("Example GmbH")

    # The FAILED account ran later, but it did not succeed, so it does not
    # make the workspace look fresh.
    assert context["last_sync_at"] == "2026-10-29 10:00:00"
    assert context["last_sync_status"] == "Complete"


def test_it_reports_no_synchronisation_when_there_has_been_none(meta_api):
    meta_api.TEST_STATE["accounts"] = [
        _account("META-1", "Never", "EUR", "", ""),
    ]

    context = meta_api.get_meta_ads_context("Example GmbH")

    assert context["last_sync_at"] == ""
    assert context["last_sync_status"] == ""


def test_it_says_so_when_the_account_list_is_truncated(meta_api):
    # The read asks for one more than the limit, so the read itself can tell
    # that it is short. A silently shortened list reads as "these are all the
    # accounts".
    meta_api.TEST_STATE["accounts"] = [
        _account(f"META-{index}", f"Account {index}") for index in range(meta_api.MAX_ACCOUNTS + 1)
    ]

    context = meta_api.get_meta_ads_context("Example GmbH")

    assert context["accounts_truncated"] is True
    assert len(context["accounts"]) == meta_api.MAX_ACCOUNTS
