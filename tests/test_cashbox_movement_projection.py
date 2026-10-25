"""Integration coverage for the cashbox rollup's standalone movement projection."""

from __future__ import annotations

import importlib
import sys
import types
from decimal import Decimal
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.laratik_cashbox.doctype.laratik_cash_box.laratik_cash_box"


class FakeDocument:
    def __init__(self, **values):
        self.__dict__.update(values)

    def get(self, field):
        return getattr(self, field, None)

    def logger(self):
        return SimpleNamespace(warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None)

    def db_set(self, values, update_modified=False):
        self.saved_rollup = values


@pytest.fixture
def cashbox_module(monkeypatch):
    saved = {key: sys.modules.get(key) for key in ("frappe", "frappe.model", "frappe.model.document")}
    fake = types.ModuleType("frappe")
    fake.throw = lambda message: (_ for _ in ()).throw(AssertionError(str(message)))
    fake_rows = []

    def get_all(doctype, filters=None, or_filters=None, fields=None, **kwargs):
        assert doctype == "LaraTik Cash Movement"
        filters = filters or {}
        matches = []
        for row in fake_rows:
            failed = False
            for key, value in filters.items():
                if isinstance(value, list) and value[:1] == ["is"]:
                    failed = failed or (value[1] == "set" and not row.get(key))
                elif isinstance(value, list) and value[:1] == ["in"]:
                    failed = failed or row.get(key) not in value[1]
                elif isinstance(value, list) and value[:1] == [">"]:
                    failed = failed or row.get(key, "") <= value[1]
                else:
                    failed = failed or row.get(key) != value
            if failed:
                continue
            if or_filters and not any(
                all(row.get(key) == value for key, value in group.items())
                for group in or_filters
            ):
                continue
            matches.append(SimpleNamespace(**{field: row.get(field) for field in (fields or row.keys())}))
        return matches[:kwargs.get("limit_page_length", len(matches))]

    fake.get_all = get_all
    fake.db = SimpleNamespace(
        get_value=lambda doctype, name, fields=None, as_dict=False: (
            {"account_currency": "USD", "company": "Laratik LLC"}
            if doctype == "Account" and as_dict
            else ("USD" if doctype == "Company" else None)
        )
    )
    fake_model = types.ModuleType("frappe.model")
    fake_document_module = types.ModuleType("frappe.model.document")
    fake_document_module.Document = FakeDocument
    fake_model.document = fake_document_module
    fake.model = fake_model
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.model", fake_model)
    monkeypatch.setitem(sys.modules, "frappe.model.document", fake_document_module)
    sys.modules.pop(MODULE, None)
    module = importlib.import_module(MODULE)
    yield module, fake, fake_rows
    sys.modules.pop(MODULE, None)
    for key, value in saved.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


def movement(**overrides):
    return {
        "name": "MOVE-1",
        "movement_date": "2026-10-23",
        "source_cash_box": "BOX-A",
        "destination_cash_box": None,
        "direction": "Out",
        "amount": Decimal("30.25"),
        "currency": "USD",
        "category": "Cash",
        "variance_type": None,
        "legacy_entry_name": None,
        "migration_state": "Native",
        "docstatus": 1,
        **overrides,
    }


def cashbox(module, name, entries=()):
    return module.LaraTikCashBox(
        name=name,
        cash_account=f"ACCOUNT-{name}",
        balance_rule="Received minus distributed",
        entries=list(entries),
        pending_items=[],
    )


def test_submitted_standalone_movement_is_projected_into_source_rollup(cashbox_module):
    module, _fake, rows = cashbox_module
    rows.append(movement())

    result = cashbox(module, "BOX-A").compute_rollup()

    assert result["total_cash"] == Decimal("-30.25")
    assert result["total_distributed"] == Decimal("30.25")


def test_transfer_updates_destination_and_mirrored_legacy_row_counts_once(cashbox_module):
    module, _fake, rows = cashbox_module
    rows.append(movement(
        destination_cash_box="BOX-B",
        legacy_entry_name="LEGACY-1",
    ))
    legacy_row = {
        "name": "LEGACY-1",
        "entry_date": "2026-10-23",
        "direction": "Out",
        "amount": Decimal("30.25"),
        "currency": "USD",
        "category": "Cash",
        "status": "Completed",
        "approval_status": "Approved",
        "other_box": "BOX-B",
    }

    source = cashbox(module, "BOX-A", [legacy_row]).compute_rollup()
    destination = cashbox(module, "BOX-B").compute_rollup()

    assert source["total_cash"] == Decimal("-30.25")
    assert destination["total_cash"] == Decimal("30.25")
    assert source["total_distributed"] == Decimal("0")
    assert destination["total_received"] == Decimal("0")


def test_unverified_or_draft_mirror_does_not_hide_legacy_completed_row(cashbox_module):
    module, _fake, rows = cashbox_module
    rows.append(movement(
        name="MOVE-DRAFT",
        legacy_entry_name="LEGACY-1",
        migration_state="Unverified",
        docstatus=0,
    ))
    legacy_row = {
        "name": "LEGACY-1", "entry_date": "2026-10-23", "direction": "Out",
        "amount": Decimal("30.25"), "currency": "USD", "category": "Cash",
        "status": "Completed", "approval_status": "Approved",
    }

    result = cashbox(module, "BOX-A", [legacy_row]).compute_rollup()

    assert result["total_cash"] == Decimal("-30.25")
    assert result["total_distributed"] == Decimal("30.25")


def test_unreadable_amount_clears_all_persisted_derived_fields(cashbox_module):
    module, _fake, _rows = cashbox_module
    box = cashbox(module, "BOX-A", [{
        "name": "LEGACY-BAD", "entry_date": "2026-10-23", "direction": "Out",
        "amount": None, "currency": "USD", "category": "Cash",
        "status": "Completed", "approval_status": "Approved",
    }])

    values = box.compute_rollup()
    persisted = box.apply_rollup()

    assert values == {field: None for field in module.DERIVED_FIELDS}
    assert box.saved_rollup == values
