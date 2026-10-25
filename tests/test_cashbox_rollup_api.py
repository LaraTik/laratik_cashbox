"""Tests for the bounded, permission-checked cashbox rollup API."""

import importlib
import sys
import types
from decimal import Decimal
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.api"


@pytest.fixture
def rollup_api(monkeypatch):
    saved = sys.modules.get("frappe")
    fake = types.ModuleType("frappe")
    docs = {}
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.get_doc = lambda doctype, name: docs[(doctype, name)]
    fake.docs = docs
    monkeypatch.setitem(sys.modules, "frappe", fake)
    sys.modules.pop(MODULE, None)
    api = importlib.import_module(MODULE)
    yield api, fake
    sys.modules.pop(MODULE, None)
    if saved is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = saved


class FakeDoc:
    def __init__(self, **values):
        self.__dict__.update(values)
        self.permission_checks = []

    def get(self, field):
        return getattr(self, field, None)

    def check_permission(self, permission):
        self.permission_checks.append(permission)
        if not getattr(self, "readable", True):
            raise PermissionError(f"{self.name} is not readable")


def install_box(fake, name="BOX-1", computed=None):
    box = FakeDoc(name=name, cash_account="ACCOUNT-1")
    box._box_currency = lambda: "AED"
    box.compute_rollup = lambda: computed or {
        "current_balance": Decimal("11.25"),
        "total_cash": Decimal("11.25"),
        "total_received": Decimal("20.00"),
        "total_distributed": Decimal("8.75"),
        "total_outstanding": Decimal("0"),
        "last_activity": "2026-10-23",
        "readable": True,
        "unreadable_count": 0,
    }
    account = FakeDoc(name="ACCOUNT-1", company="Example Ltd")
    company = FakeDoc(name="Example Ltd")
    fake.docs[("LaraTik Cash Box", name)] = box
    fake.docs[("Account", "ACCOUNT-1")] = account
    fake.docs[("Company", "Example Ltd")] = company
    return box, account, company


def test_rollup_api_returns_small_currency_and_company_scoped_summary(rollup_api):
    api, fake = rollup_api
    box, account, company = install_box(fake)

    result = api.get_cashbox_rollups('["BOX-1"]')

    assert result == [{
        "name": "BOX-1",
        "company": "Example Ltd",
        "currency": "AED",
        "readable": True,
        "unreadable_count": 0,
        "current_balance": Decimal("11.25"),
        "total_cash": Decimal("11.25"),
        "total_received": Decimal("20.00"),
        "total_distributed": Decimal("8.75"),
        "total_outstanding": Decimal("0"),
        "last_activity": "2026-10-23",
    }]
    assert box.permission_checks == ["read"]
    assert account.permission_checks == ["read"]
    assert company.permission_checks == ["read"]


def test_unreadable_rollup_returns_null_totals_instead_of_partial_values(rollup_api):
    api, fake = rollup_api
    install_box(fake, computed={
        "current_balance": None,
        "total_cash": None,
        "total_received": None,
        "total_distributed": None,
        "total_outstanding": None,
        "last_activity": None,
        "readable": False,
        "unreadable_count": 2,
    })

    result = api.get_cashbox_rollups(["BOX-1"])[0]

    assert result["readable"] is False
    assert result["unreadable_count"] == 2
    assert result["total_cash"] is None
    assert result["total_received"] is None


def test_rollup_api_rejects_unbounded_and_malformed_requests(rollup_api):
    api, _fake = rollup_api

    with pytest.raises(ValueError, match="JSON array"):
        api.get_cashbox_rollups("not-json")
    with pytest.raises(ValueError, match="JSON array of names"):
        api.get_cashbox_rollups({"name": "BOX-1"})
    with pytest.raises(ValueError, match="At most"):
        api.get_cashbox_rollups([f"BOX-{index}" for index in range(api.MAX_CASHBOX_ROLLUP_BATCH + 1)])


def test_rollup_api_stops_when_a_linked_company_is_not_readable(rollup_api):
    api, fake = rollup_api
    _box, _account, company = install_box(fake)
    company.readable = False

    with pytest.raises(PermissionError, match="Example Ltd is not readable"):
        api.get_cashbox_rollups(["BOX-1"])
