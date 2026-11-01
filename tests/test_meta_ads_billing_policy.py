"""The accounting policy of a Meta Ads billing profile can be SET — and only decided.

ADR-232. A policy answer is a field nobody filled in. An UNSET field means
"not decided": it is never defaulted, the app never picks one, and the invoice
draft waits for a human to settle it.

Before this endpoint the seven policy fields on `LaraTik Meta Ads Billing
Profile` could be READ by the invoice workspace but not WRITTEN from the app,
so a period could never become ready and the invoice workflow was a dead end.

The rules this test holds:

1. An admin can save a valid policy, and the profile carries it.
2. A value outside the CLOSED vocabulary is refused. The server never invents
   an answer, and it never accepts one it does not know.
3. An EMPTY value CLEARS the answer — the reader chose "not decided" — while a
   value that is NOT PROVIDED leaves the stored answer untouched. Those are
   different acts, and a caller that cannot tell them apart cannot offer the
   reader either.
4. A profile belonging to another Company is refused.
5. A reader who is not an admin is refused.
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.meta_ads_api"


class Refusal(Exception):
    """What `frappe.throw` raises in the fake."""


class FakeProfile:
    """The billing profile, with the fields the policy answers live on."""

    def __init__(self, name: str, company: str):
        self.name = name
        self.company = company
        self.charge_model = ""
        self.recognition_model = ""
        self.income_account = ""
        self.invoice_item = ""
        self.tax_treatment = ""
        self.tax_template = ""
        self.fee_is_configured = 0
        self.saved = 0
        self.deny_write = False

    def check_permission(self, permission: str):
        if permission == "write" and self.deny_write:
            raise Refusal("Not permitted.")

    def save(self):
        self.saved += 1
        return self


@pytest.fixture
def policy_api(monkeypatch):
    """Load the real API module against a small in-memory Frappe surface."""
    saved_modules = {
        name: sys.modules.get(name) for name in ("frappe", "frappe.utils", "requests")
    }

    fake = types.ModuleType("frappe")
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.throw = lambda message: (_ for _ in ()).throw(Refusal(str(message)))
    fake.only_for = lambda role: (_ for _ in ()).throw(PermissionError(role))
    fake.session = SimpleNamespace(user="Administrator")

    state = {
        "frappe": fake,
        "companies": {"Example GmbH", "Other GmbH"},
        "profile": FakeProfile("PROFILE-1", "Example GmbH"),
        "accounts": {
            "Meta Ads Income - Example": {"company": "Example GmbH", "disabled": 0},
            "Meta Ads Income - Other": {"company": "Other GmbH", "disabled": 0},
            "Meta Ads Income - Disabled": {"company": "Example GmbH", "disabled": 1},
        },
        "items": {
            "Meta Ads Advertising": {"disabled": 0},
            "Meta Ads Advertising - Disabled": {"disabled": 1},
        },
        "templates": {
            "Germany VAT": {"company": "Example GmbH", "disabled": 0},
            "Germany VAT - Disabled": {"company": "Example GmbH", "disabled": 1},
        },
    }

    def get_value(doctype, name, fieldname="name", filters=None, as_dict=False, **_kwargs):
        del filters
        if doctype == "Account":
            row = state["accounts"].get(name)
        elif doctype == "Item":
            row = state["items"].get(name)
        elif doctype == "Sales Taxes and Charges Template":
            row = state["templates"].get(name)
        else:
            row = None
        if not row:
            return None
        wanted = list(fieldname) if isinstance(fieldname, (list, tuple)) else [fieldname]
        values = {field: row.get(field) for field in wanted}
        if as_dict:
            return values
        if len(wanted) == 1:
            return values[wanted[0]]
        return tuple(values[field] for field in wanted)

    def get_doc(doctype, name=None):
        if doctype == "Company":
            return SimpleNamespace(name=name, check_permission=lambda _permission: None)
        if doctype == "LaraTik Meta Ads Billing Profile":
            return state["profile"]
        raise AssertionError(f"Unexpected get_doc({doctype!r}, {name!r})")

    fake.db = SimpleNamespace(
        get_value=get_value,
        exists=lambda doctype, name: doctype != "Company" or name in state["companies"],
    )
    fake.get_doc = get_doc
    fake.get_all = lambda *_args, **_kwargs: []

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda value: value
    utils.now_datetime = lambda: None
    requests = types.ModuleType("requests")
    requests.RequestException = RuntimeError

    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    monkeypatch.setitem(sys.modules, "requests", requests)
    sys.modules.pop(MODULE, None)

    api = importlib.import_module(MODULE)
    api.TEST_STATE = state
    yield api, state

    sys.modules.pop(MODULE, None)
    for name, saved in saved_modules.items():
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


def test_an_admin_can_save_a_valid_policy_and_the_profile_carries_it(policy_api):
    api, state = policy_api

    result = api.save_billing_policy(
        "Example GmbH",
        "PROFILE-1",
        charge_model="Recharge",
        recognition_model="Principal",
        income_account="Meta Ads Income - Example",
        invoice_item="Meta Ads Advertising",
        tax_treatment="Template",
        tax_template="Germany VAT",
        fee_is_configured=1,
    )

    profile = state["profile"]
    assert profile.charge_model == "Recharge"
    assert profile.recognition_model == "Principal"
    assert profile.income_account == "Meta Ads Income - Example"
    assert profile.invoice_item == "Meta Ads Advertising"
    assert profile.tax_treatment == "Template"
    assert profile.tax_template == "Germany VAT"
    assert profile.fee_is_configured == 1
    assert profile.saved == 1
    assert result["profile"] == "PROFILE-1"
    assert result["charge_model"] == "Recharge"
    assert result["fee_is_configured"] == 1


def test_a_value_outside_the_vocabulary_is_refused(policy_api):
    api, state = policy_api

    with pytest.raises(Refusal, match="charge model"):
        api.save_billing_policy("Example GmbH", "PROFILE-1", charge_model="Gross margin")

    # Nothing was stored, and nothing was saved.
    assert state["profile"].charge_model == ""
    assert state["profile"].saved == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("charge_model", "Gross margin"),
        ("recognition_model", "Reseller"),
        ("tax_treatment", "Reverse charge"),
    ],
)
def test_every_closed_vocabulary_is_closed(policy_api, field, value):
    api, state = policy_api

    with pytest.raises(Refusal):
        api.save_billing_policy("Example GmbH", "PROFILE-1", **{field: value})

    assert state["profile"].saved == 0


def test_an_empty_value_clears_the_answer_and_an_absent_one_leaves_it_alone(policy_api):
    api, state = policy_api

    api.save_billing_policy(
        "Example GmbH",
        "PROFILE-1",
        charge_model="Recharge",
        recognition_model="Agent",
        tax_treatment="Template",
        fee_is_configured=1,
    )
    assert state["profile"].charge_model == "Recharge"

    # The reader changed their mind about ONE answer.
    api.save_billing_policy("Example GmbH", "PROFILE-1", charge_model="")

    # The answer they took back is gone, and the answers they did not touch
    # are still there: a caller that answers one question must not silently
    # clear the others.
    assert state["profile"].charge_model == ""
    assert state["profile"].recognition_model == "Agent"
    assert state["profile"].tax_treatment == "Template"
    assert state["profile"].fee_is_configured == 1


def test_the_server_never_defaults_a_policy_answer(policy_api):
    api, state = policy_api

    api.save_billing_policy("Example GmbH", "PROFILE-1", charge_model="Recharge")

    profile = state["profile"]
    assert profile.charge_model == "Recharge"
    # Everything nobody answered is still UNSET. The server does not pick.
    assert profile.recognition_model == ""
    assert profile.income_account == ""
    assert profile.invoice_item == ""
    assert profile.tax_treatment == ""
    assert profile.tax_template == ""
    assert profile.fee_is_configured == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("income_account", "Meta Ads Income - Missing"),
        ("income_account", "Meta Ads Income - Disabled"),
        ("income_account", "Meta Ads Income - Other"),
        ("invoice_item", "Meta Ads Advertising - Missing"),
        ("invoice_item", "Meta Ads Advertising - Disabled"),
        ("tax_template", "Germany VAT - Missing"),
        ("tax_template", "Germany VAT - Disabled"),
    ],
)
def test_a_link_must_name_a_record_that_exists_and_is_enabled(policy_api, field, value):
    api, state = policy_api

    with pytest.raises(Refusal):
        api.save_billing_policy("Example GmbH", "PROFILE-1", **{field: value})

    assert state["profile"].saved == 0


def test_a_profile_from_another_company_is_refused(policy_api):
    api, state = policy_api
    state["profile"].company = "Other GmbH"

    with pytest.raises(Refusal, match="another Company"):
        api.save_billing_policy("Example GmbH", "PROFILE-1", charge_model="Recharge")

    assert state["profile"].saved == 0


def test_a_reader_who_is_not_an_admin_is_refused(policy_api):
    api, state = policy_api
    state["frappe"].session.user = "viewer@example.com"

    with pytest.raises(PermissionError):
        api.save_billing_policy("Example GmbH", "PROFILE-1", charge_model="Recharge")

    assert state["profile"].saved == 0


def test_a_reader_who_may_not_write_the_profile_is_refused(policy_api):
    api, state = policy_api
    state["profile"].deny_write = True

    with pytest.raises(Refusal):
        api.save_billing_policy("Example GmbH", "PROFILE-1", charge_model="Recharge")

    assert state["profile"].saved == 0
