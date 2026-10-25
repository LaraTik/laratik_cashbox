"""Scoped master-data operations for the first-use CRM flow."""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.api"


class FakeDoc:
    def __init__(self, fake, values):
        self.fake = fake
        self.values = values
        self.name = "CUST-00001" if values["doctype"] == "Customer" else "SUP-00001"

    def insert(self, ignore_permissions=False):
        self.fake.inserted.append((self.values, ignore_permissions))
        return self


@pytest.fixture
def crm_api(monkeypatch):
    saved = sys.modules.get("frappe")
    fake = types.ModuleType("frappe")
    fake.session = SimpleNamespace(user="setup@laratik.local")
    fake.roles = {"System Manager"}
    fake.calls = []
    fake.inserted = []
    fake.existing = {}
    fake.references = {
        ("Customer Group", "Businesses"): {"is_group": 0},
        ("Supplier Group", "Services"): {"is_group": 0},
        ("Territory", "Germany"): {"is_group": 0},
        ("Currency", "EUR"): {"enabled": 1},
        ("Currency", "USD"): {"enabled": 1},
    }
    fake.rows = {
        "Customer Group": [{"name": "Businesses"}],
        "Supplier Group": [{"name": "Services"}],
        "Territory": [{"name": "Germany"}],
        "Currency": [{"name": "EUR", "currency_name": "Euro"}],
    }
    fake.type_options = {
        "Customer": {"customer_type": "Company\nIndividual\nPartnership"},
        "Supplier": {"supplier_type": "Company\nIndividual\nPartnership"},
    }
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.only_for = lambda role: (
        None if role in fake.roles else (_ for _ in ()).throw(PermissionError(role))
    )
    fake.db = SimpleNamespace(
        exists=lambda doctype, name: (doctype, name) in fake.references,
        get_value=lambda doctype, name, field: fake.references.get((doctype, name), {}).get(field),
    )
    fake.get_meta = lambda doctype: SimpleNamespace(
        get_field=lambda field: SimpleNamespace(options=fake.type_options[doctype][field])
    )

    def get_all(doctype, **kwargs):
        fake.calls.append((doctype, kwargs))
        if doctype in fake.existing:
            return fake.existing[doctype]
        rows = fake.rows.get(doctype, [])
        if kwargs.get("filters", {}).get("is_group") == 0:
            return rows
        return rows

    fake.get_all = get_all
    fake.get_doc = lambda values: FakeDoc(fake, values)
    monkeypatch.setitem(sys.modules, "frappe", fake)
    sys.modules.pop(MODULE, None)
    api = importlib.import_module(MODULE)
    yield api, fake
    sys.modules.pop(MODULE, None)
    if saved is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = saved


def test_crm_setup_options_return_only_leaf_references_and_enabled_currencies(crm_api):
    api, fake = crm_api

    options = api.get_crm_setup_options("Customer")

    assert options == {
        "party_type": "Customer",
        "type_options": ["Company", "Individual", "Partnership"],
        "groups": [{"name": "Businesses"}],
        "territories": [{"name": "Germany"}],
        "currencies": [{"name": "EUR", "currency_name": "Euro"}],
    }
    assert [call[0] for call in fake.calls] == [
        "Customer Group",
        "Territory",
        "Currency",
    ]


def test_crm_party_creation_uses_only_supported_master_fields(crm_api):
    api, fake = crm_api

    result = api.create_crm_party(
        "Customer",
        {
            "name": "Just Halal GmbH",
            "type": "Company",
            "group": "Businesses",
            "territory": "Germany",
            "currency": "EUR",
            "email": "accounts@example.test",
        },
    )

    assert result == {"name": "CUST-00001", "already_existed": False}
    assert fake.inserted == [
        (
            {
                "doctype": "Customer",
                "customer_name": "Just Halal GmbH",
                "customer_type": "Company",
                "customer_group": "Businesses",
                "territory": "Germany",
                "default_currency": "EUR",
                "email_id": "accounts@example.test",
            },
            True,
        )
    ]


def test_crm_party_creation_refuses_financial_or_unknown_fields(crm_api):
    api, fake = crm_api

    with pytest.raises(ValueError, match="[Uu]nsupported"):
        api.create_crm_party(
            "Customer",
            {"name": "Fixture", "type": "Company", "opening_balance": 100},
        )

    assert fake.inserted == []


def test_crm_party_creation_is_idempotent_for_an_identical_retry(crm_api):
    api, fake = crm_api
    values = {
        "name": "Just Halal GmbH",
        "type": "Company",
        "group": "Businesses",
        "territory": "Germany",
        "currency": "EUR",
        "email": "accounts@example.test",
    }
    fake.existing["Customer"] = [
        {
            "name": "CUST-00001",
            "customer_name": values["name"],
            "customer_type": values["type"],
            "customer_group": values["group"],
            "territory": values["territory"],
            "default_currency": values["currency"],
            "email_id": values["email"],
        }
    ]

    result = api.create_crm_party("Customer", values)

    assert result == {"name": "CUST-00001", "already_existed": True}
    assert fake.inserted == []


def test_crm_party_creation_refuses_a_duplicate_name_with_different_details(crm_api):
    api, fake = crm_api
    fake.existing["Customer"] = [
        {
            "name": "CUST-00001",
            "customer_name": "Just Halal GmbH",
            "customer_type": "Company",
            "customer_group": "Businesses",
            "territory": "Germany",
            "default_currency": "USD",
            "email_id": "accounts@example.test",
        }
    ]

    result = api.create_crm_party(
        "Customer",
        {
            "name": "Just Halal GmbH",
            "type": "Company",
            "group": "Businesses",
            "territory": "Germany",
            "currency": "EUR",
            "email": "accounts@example.test",
        },
    )

    assert result == {"error_code": "CRM_CONFLICT"}
    assert fake.inserted == []


def test_crm_party_creation_requires_existing_leaf_references(crm_api):
    api, fake = crm_api
    fake.references[("Customer Group", "Parent Group")] = {"is_group": 1}

    with pytest.raises(ValueError, match="leaf group"):
        api.create_crm_party(
            "Customer",
            {
                "name": "Fixture",
                "type": "Company",
                "group": "Parent Group",
                "territory": "Germany",
            },
        )

    assert fake.inserted == []


def test_crm_master_data_methods_require_system_manager(crm_api):
    api, fake = crm_api
    fake.roles = set()

    with pytest.raises(PermissionError, match="System Manager"):
        api.get_crm_setup_options("Customer")
    with pytest.raises(PermissionError, match="System Manager"):
        api.create_crm_party("Customer", {"name": "Fixture", "type": "Company"})

    assert fake.calls == []
    assert fake.inserted == []
