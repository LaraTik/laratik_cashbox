"""Server-controller invariants for the standalone cash movement DocType."""

from __future__ import annotations

import importlib
import sys
import types
from decimal import Decimal
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.laratik_cashbox.doctype.laratik_cash_movement.laratik_cash_movement"


class FakeFrappeThrow(Exception):
    pass


class FakeDocument:
    def __init__(self, **values):
        self.__dict__.update(values)

    def precision(self, fieldname):
        return getattr(self, f"{fieldname}_precision", 2)


class FakeDB:
    def get_value(self, doctype, name, fields=None, as_dict=False):
        if doctype == "LaraTik Cash Box":
            result = {"cash_account": f"ACCOUNT-{name}"}
        elif doctype == "Account":
            result = {"company": "LaraTik LLC", "account_currency": "USD"}
        elif doctype == "Company":
            result = "USD"
        elif doctype == "Employee":
            result = "LarTik LLC"
        else:
            raise AssertionError(f"unexpected fixture query {doctype}.{fields}")
        if isinstance(result, dict) and as_dict:
            return SimpleNamespace(**result)
        return result if not isinstance(result, dict) else result.get(fields)

    def exists(self, doctype, name):
        if doctype == "Supplier":
            return name == "SUP-1" or name == {"supplier_name": "Acme"}
        if doctype == "Employee":
            return name == "EMP-1" or name == {"employee_name": "Employee One"}
        return False


@pytest.fixture
def movement_controller():
    saved = {key: sys.modules.get(key) for key in ("frappe", "frappe.model", "frappe.model.document")}
    fake = types.ModuleType("frappe")
    fake.db = FakeDB()
    fake.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(FakeFrappeThrow(str(message)))
    fake.whitelist = lambda *args, **kwargs: (lambda function: function)
    fake.get_doc = lambda doctype, name: SimpleNamespace(
        docstatus=1, company="LaraTik LLC", currency="USD", source_cash_box="BOX-A",
        destination_cash_box=None, direction="Out", amount=Decimal("25.00"),
    )
    fake_model = types.ModuleType("frappe.model")
    fake_document = types.ModuleType("frappe.model.document")
    fake_document.Document = FakeDocument
    fake_model.document = fake_document
    fake.model = fake_model
    sys.modules.update({"frappe": fake, "frappe.model": fake_model, "frappe.model.document": fake_document})
    try:
        module = importlib.import_module(MODULE)
        yield importlib.reload(module)
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def valid_movement(**overrides):
    values = {
        "name": "MOVE-1",
        "idempotency_key": "stable-key-123",
        "movement_date": "2026-10-23",
        "company": None,
        "source_cash_box": "BOX-A",
        "destination_cash_box": None,
        "direction": "Out",
        "amount": Decimal("25.00"),
        "currency": "USD",
        "category": "Cash",
        "counterparty_type": None,
        "counterparty": None,
        "counterparty_text": None,
        "variance_type": None,
        "variance_amount": None,
        "migration_state": "Native",
        "workflow_state": "Draft",
        "reversal_of": None,
        "accounting_voucher_type": None,
        "accounting_voucher": None,
    }
    values.update(overrides)
    return values


def test_valid_movement_derives_company_from_linked_account(movement_controller):
    doc = movement_controller.LaraTikCashMovement(**valid_movement())

    doc.validate()

    assert doc.company == "LaraTik LLC"


def test_amount_uses_the_effective_erpnext_field_precision(movement_controller):
    doc = movement_controller.LaraTikCashMovement(
        **valid_movement(amount=Decimal("1.234"), amount_precision=3)
    )

    doc.validate()

    assert doc.amount == Decimal("1.234")


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"idempotency_key": ""}, "idempotency key"),
        ({"amount": Decimal("0")}, "greater than zero"),
        ({"amount": Decimal("NaN")}, "greater than zero"),
        ({"amount": Decimal("Infinity")}, "greater than zero"),
        ({"amount": Decimal("1.001")}, "exceeds ERPNext precision of 2 decimal places"),
        ({"company": "Other Co"}, "company must match"),
        ({"currency": "EUR"}, "currency must match"),
        ({"counterparty_type": "Contact"}, "Customer, Supplier, or Employee"),
        ({"variance_amount": Decimal("2.00")}, "requires Overpayment or Underpayment"),
        ({"variance_type": "Underpayment"}, "requires a nonzero variance"),
        ({"variance_amount": Decimal("-1.00"), "variance_type": "Underpayment"}, "must not be negative"),
        ({"variance_amount": Decimal("Infinity"), "variance_type": "Underpayment"}, "finite decimal"),
        ({"variance_amount": Decimal("0.001"), "variance_type": "Underpayment"}, "exceeds ERPNext precision of 2 decimal places"),
    ],
)
def test_invalid_movement_is_refused(movement_controller, overrides, message):
    doc = movement_controller.LaraTikCashMovement(**valid_movement(**overrides))

    with pytest.raises(FakeFrappeThrow, match=message):
        doc.validate()


def test_transfer_requires_distinct_boxes_and_outgoing_source(movement_controller):
    same_box = movement_controller.LaraTikCashMovement(
        **valid_movement(destination_cash_box="BOX-A")
    )
    with pytest.raises(FakeFrappeThrow, match="two different cash boxes"):
        same_box.validate()

    inbound_source = movement_controller.LaraTikCashMovement(
        **valid_movement(destination_cash_box="BOX-B", direction="In")
    )
    with pytest.raises(FakeFrappeThrow, match="outgoing source movement"):
        inbound_source.validate()


def test_counterparty_link_and_fallback_text_are_mutually_exclusive(movement_controller):
    doc = movement_controller.LaraTikCashMovement(**valid_movement(
        counterparty_type="Supplier", counterparty="SUP-1", counterparty_text="Other name"
    ))

    with pytest.raises(FakeFrappeThrow, match="either a linked ERPNext party"):
        doc.validate()


def test_known_counterparty_must_use_master_link_instead_of_free_text(movement_controller):
    doc = movement_controller.LaraTikCashMovement(**valid_movement(counterparty_text="Acme"))

    with pytest.raises(FakeFrappeThrow, match="matches an ERPNext party master"):
        doc.validate()


def test_employee_counterparty_must_match_movement_company(movement_controller, monkeypatch):
    monkeypatch.setattr(
        movement_controller.frappe.db,
        "get_value",
        lambda doctype, name, fields=None, as_dict=False: "Other Co" if doctype == "Employee" else FakeDB().get_value(
            doctype, name, fields, as_dict
        ),
    )
    doc = movement_controller.LaraTikCashMovement(**valid_movement(
        counterparty_type="Employee", counterparty="EMP-1"
    ))

    with pytest.raises(FakeFrappeThrow, match="Employee counterparty must belong"):
        doc.validate()


def test_creator_cannot_approve_or_reject_even_as_administrator(movement_controller):
    movement_controller.frappe.session = SimpleNamespace(user="Administrator")
    doc = movement_controller.LaraTikCashMovement(**valid_movement())
    doc.owner = "Administrator"
    doc._doc_before_save = SimpleNamespace(workflow_state="Draft")
    doc.workflow_state = "Approved"

    with pytest.raises(FakeFrappeThrow, match="creator cannot approve or reject"):
        doc._validate_approval_separation()


def test_financial_fields_are_locked_until_the_workflow_returns_to_draft(movement_controller):
    before = valid_movement(workflow_state="Approved")
    doc = movement_controller.LaraTikCashMovement(**valid_movement(workflow_state="Approved"))
    doc._doc_before_save = dict(before)
    doc.amount = Decimal("26.00")

    with pytest.raises(FakeFrappeThrow, match="locked after approval is requested"):
        doc._validate_approved_values_unchanged()

    doc.amount = before["amount"]
    doc.workflow_state = "Draft"
    doc._validate_approved_values_unchanged()


def test_unverified_or_unapproved_documents_cannot_submit(movement_controller):
    unverified = movement_controller.LaraTikCashMovement(
        **valid_movement(migration_state="Unverified", workflow_state="Submitted")
    )
    with pytest.raises(FakeFrappeThrow, match="unverified legacy mirror"):
        unverified.before_submit()

    draft = movement_controller.LaraTikCashMovement(**valid_movement(workflow_state="Approved"))
    with pytest.raises(FakeFrappeThrow, match="must pass .* Approval workflow"):
        draft.before_submit()


def test_submitted_movement_must_be_reversed_not_cancelled(movement_controller):
    doc = movement_controller.LaraTikCashMovement(**valid_movement())
    with pytest.raises(FakeFrappeThrow, match="Create a linked reversal"):
        doc.on_cancel()


@pytest.mark.parametrize("voucher_type", ["Custom Journal", "Bank Entry"])
def test_voucher_reference_rejects_nonstandard_doctypes(movement_controller, voucher_type):
    doc = movement_controller.LaraTikCashMovement(
        **valid_movement(accounting_voucher_type=voucher_type, accounting_voucher="JV-1")
    )
    with pytest.raises(FakeFrappeThrow, match="supported standard ERPNext accounting voucher"):
        doc._validate_voucher()


def test_journal_entry_is_the_standard_reference_for_bank_entry_vouchers(movement_controller, monkeypatch):
    monkeypatch.setattr(
        movement_controller.frappe.db,
        "get_value",
        lambda doctype, name, fields=None, as_dict=False: SimpleNamespace(
            docstatus=1, company="LaraTik LLC"
        ) if doctype == "Journal Entry" else FakeDB().get_value(doctype, name, fields, as_dict),
    )
    doc = movement_controller.LaraTikCashMovement(
        **valid_movement(accounting_voucher_type="Journal Entry", accounting_voucher="JV-1")
    )
    doc.company = "LaraTik LLC"

    doc._validate_voucher()


def test_voucher_reference_requires_a_submitted_same_company_voucher(movement_controller, monkeypatch):
    monkeypatch.setattr(
        movement_controller.frappe.db,
        "get_value",
        lambda doctype, name, fields=None, as_dict=False: SimpleNamespace(
            docstatus=1, company="LaraTik LLC"
        ) if doctype == "Payment Entry" else FakeDB().get_value(doctype, name, fields, as_dict),
    )
    doc = movement_controller.LaraTikCashMovement(
        **valid_movement(accounting_voucher_type="Payment Entry", accounting_voucher="PAY-1")
    )
    doc.company = "LaraTik LLC"
    doc._validate_voucher()

    with pytest.raises(FakeFrappeThrow, match="submitted ERPNext voucher"):
        monkeypatch.setattr(
            movement_controller.frappe.db,
            "get_value",
            lambda *args, **kwargs: SimpleNamespace(docstatus=0, company="LaraTik LLC"),
        )
        doc._validate_voucher()

