"""The initial setup account list is limited to one readable Company."""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.api"
FIELDS = ["name", "account_name", "account_type", "root_type", "account_currency"]


class FakeCompany:
    def __init__(self, name, readable=True):
        self.name = name
        self.readable = readable
        self.permission_checks = []

    def check_permission(self, permission):
        self.permission_checks.append(permission)
        if not self.readable:
            raise PermissionError(f"{self.name} is not readable")


@pytest.fixture
def setup_accounts_api(monkeypatch):
    saved = sys.modules.get("frappe")
    fake = types.ModuleType("frappe")
    fake.session = SimpleNamespace(user="setup@laratik.local")
    fake.roles = {"System Manager"}
    fake.company = FakeCompany("Example Ltd")
    fake.account_rows = [
        {"name": "Cash - EX", "account_name": "Cash", "root_type": "Asset"},
        {"name": "Service Income - EX", "account_name": "Service Income", "root_type": "Income"},
    ]
    fake.account_records = {
        "Cash - EX": {"company": "Example Ltd", "is_group": 0, "root_type": "Asset"},
    }
    fake.calls = []
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.only_for = lambda role: (
        None if role in fake.roles else (_ for _ in ()).throw(PermissionError(role))
    )
    fake.db = SimpleNamespace(
        exists=lambda doctype, name: doctype == "Company" and name == fake.company.name,
        get_value=lambda doctype, name, fields, as_dict=False: (
            fake.account_records.get(name) if doctype == "Account" else None
        ),
    )
    fake.get_doc = lambda doctype, name: fake.company

    def get_all(doctype, **kwargs):
        fake.calls.append((doctype, kwargs))
        return fake.account_rows

    fake.get_all = get_all
    monkeypatch.setitem(sys.modules, "frappe", fake)
    sys.modules.pop(MODULE, None)
    api = importlib.import_module(MODULE)
    yield api, fake
    sys.modules.pop(MODULE, None)
    if saved is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = saved


def test_setup_accounts_are_scoped_to_one_readable_company(setup_accounts_api):
    api, fake = setup_accounts_api

    result = api.get_setup_accounts("  Example Ltd  ")

    assert result == fake.account_rows
    assert fake.company.permission_checks == ["read"]
    assert fake.calls == [
        (
            "Account",
            {
                "filters": {"company": "Example Ltd", "is_group": 0},
                "fields": FIELDS,
                "order_by": "name asc",
                "limit_page_length": 2000,
            },
        )
    ]


def test_setup_accounts_refuse_an_unknown_company_before_querying(setup_accounts_api):
    api, fake = setup_accounts_api

    with pytest.raises(ValueError, match="Choose an existing Company"):
        api.get_setup_accounts("Other Ltd")

    assert fake.calls == []


def test_setup_accounts_refuse_an_unreadable_company(setup_accounts_api):
    api, fake = setup_accounts_api
    fake.company.readable = False

    with pytest.raises(PermissionError, match="Example Ltd is not readable"):
        api.get_setup_accounts("Example Ltd")

    assert fake.calls == []


def test_setup_accounts_require_system_manager(setup_accounts_api):
    api, fake = setup_accounts_api
    fake.session.user = "viewer@laratik.local"
    fake.roles = set()

    with pytest.raises(PermissionError, match="System Manager"):
        api.get_setup_accounts("Example Ltd")

    assert fake.calls == []


def test_setup_state_account_mapping_uses_company_scoped_metadata(setup_accounts_api):
    api, fake = setup_accounts_api
    state = api.normalize_setup_state(
        {"company": {"company_name": "Example Ltd"}, "account_mappings": {"company_cash": "Cash - EX"}}
    )

    api._validate_setup_state(state)


@pytest.mark.parametrize(
    "account_record,message",
    [
        (None, "does not exist"),
        ({"company": "Other Ltd", "is_group": 0, "root_type": "Asset"}, "belong"),
        ({"company": "Example Ltd", "is_group": 1, "root_type": "Asset"}, "group account"),
        ({"company": "Example Ltd", "is_group": 0, "root_type": "Liability"}, "Asset account"),
    ],
)
def test_setup_state_rejects_missing_cross_company_group_or_wrong_root_accounts(
    setup_accounts_api, account_record, message
):
    api, fake = setup_accounts_api
    fake.account_records["Cash - EX"] = account_record
    state = api.normalize_setup_state(
        {"company": {"company_name": "Example Ltd"}, "account_mappings": {"company_cash": "Cash - EX"}}
    )

    with pytest.raises(ValueError, match=message):
        api._validate_setup_state(state)
