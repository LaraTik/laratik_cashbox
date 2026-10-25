"""Read-only census rules for migration source, currency, and transfer mapping."""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.migration"


class FakeDB:
    def __init__(self, account_currency="USD"):
        self.account_currency = account_currency
        self.count_calls = []
        self.target_rows = []

    def get_value(self, doctype, name, fields=None, as_dict=False):
        if doctype == "LaraTik Cash Movement":
            target = next((row for row in self.target_rows if row.name == name), None)
            if target is None:
                return None
            if as_dict:
                return target
            return getattr(target, fields, None) if isinstance(fields, str) else target
        if doctype == "LaraTik Cash Box":
            return f"ACCOUNT-{name}"
        if doctype == "Account":
            return SimpleNamespace(company="LaraTik LLC", account_currency=self.account_currency)
        if doctype == "Company":
            return "USD"
        raise AssertionError(f"unexpected fixture read: {doctype}.{fields}")

    def count(self, doctype, filters=None):
        self.count_calls.append((doctype, filters))
        if doctype == "LaraTik Cash Movement":
            return len(self.target_rows)
        return 1


@pytest.fixture
def migration(monkeypatch):
    module_names = ("frappe", "frappe.model", "frappe.model.meta")
    saved = {name: sys.modules.get(name) for name in module_names}
    fake = types.ModuleType("frappe")
    fake.db = FakeDB()
    fake.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(RuntimeError(str(message)))
    fake.get_meta = lambda doctype: SimpleNamespace(get_field=lambda fieldname: fieldname)
    target_rows = []
    fake.db.target_rows = target_rows
    source_row = SimpleNamespace(
        name="ENTRY-1", parent="BOX-A", entry_date="2026-10-20", direction="Out",
        amount="10.25", currency="USD", category="Cash", money_flow="Other",
        funding_source="Company Cash",
        status="Completed", approval_status="Approved", counterparty=None,
        variance_type=None, variance_amount=None, purpose=None, notes=None,
        other_box=None, reversal_of=None, reference_doctype=None, reference_name=None,
        idempotency_key=None, owner="cashier@example.invalid",
    )
    fake.get_all = lambda doctype, **kwargs: target_rows if doctype == "LaraTik Cash Movement" else [source_row]
    fake_model = types.ModuleType("frappe.model")
    fake_meta = types.ModuleType("frappe.model.meta")
    fake_meta.get_field_precision = lambda field, doc=None, currency=None: 2
    fake_model.meta = fake_meta
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.model", fake_model)
    monkeypatch.setitem(sys.modules, "frappe.model.meta", fake_meta)
    try:
        module = importlib.import_module(MODULE)
        yield module, fake, source_row, target_rows
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        sys.modules.pop(MODULE, None)


def test_company_currency_fallback_is_labeled_as_erpnext_resolution(migration):
    module, fake, row, _targets = migration
    fake.db.account_currency = ""
    row.currency = None

    result = module.census()

    assert result["rows"][0]["state"] == "Verified"
    assert result["rows"][0]["currency"] == "USD"
    assert result["rows"][0]["currency_source"] == "ERPNext Account currency fallback: Company.default_currency"


def test_source_currency_conflict_is_unverified_not_overwritten(migration):
    module, fake, row, _targets = migration
    fake.db.account_currency = "USD"
    row.currency = "EUR"

    result = module.census()

    assert result["rows"][0]["state"] == "Unverified"
    assert result["rows"][0]["currency"] == "EUR"
    assert "conflicts" in result["rows"][0]["reason"]


def test_inbound_legacy_transfer_is_mapped_to_canonical_outgoing_source(migration):
    module, fake, row, _targets = migration
    row.direction = "In"
    row.other_box = "BOX-SOURCE"

    result = module.census()

    item = result["rows"][0]
    assert item["source_cash_box"] == "BOX-SOURCE"
    assert item["destination_cash_box"] == "BOX-A"
    assert item["mappable"] is True


@pytest.mark.parametrize("amount", ["0", "-10.25"])
def test_nonpositive_legacy_amount_is_unverified_and_not_migrated_as_a_movement(migration, amount):
    module, _fake, row, _targets = migration
    row.amount = amount

    item = module.census()["rows"][0]

    assert item["state"] == "Unverified"
    assert item["mappable"] is False
    assert "positive amount" in item["reason"]


def test_unknown_legacy_status_is_not_defaulted_to_completed(migration):
    module, _fake, row, _targets = migration
    row.status = "Archived"

    item = module.census()["rows"][0]

    assert item["state"] == "Unverified"
    assert item["mappable"] is True
    assert "status is unknown" in item["reason"]


@pytest.mark.parametrize(
    "field, value, reason",
    [
        ("category", "Unmapped Category", "category is not supported"),
        ("status", None, "status is missing"),
        ("approval_status", "Pending", "conflicts with its pending or rejected approval"),
    ],
)
def test_conflicting_or_unrepresentable_legacy_facts_are_unverified(migration, field, value, reason):
    module, _fake, row, _targets = migration
    setattr(row, field, value)

    item = module.census()["rows"][0]

    assert item["state"] == "Unverified"
    assert reason in item["reason"]
    if field == "category":
        assert item["mappable"] is False


def test_unknown_legacy_funding_source_is_unverified_and_not_coerced(migration):
    module, _fake, row, _targets = migration
    row.funding_source = "Unrecognized Legacy Value"

    item = module.census()["rows"][0]

    assert item["state"] == "Unverified"
    assert "funding source is unknown" in item["reason"]


def test_reversal_is_not_linked_to_an_unsubmitted_original_mirror(migration):
    module, _fake, row, targets = migration
    row.reversal_of = "ENTRY-ORIGINAL"
    targets.extend([
        SimpleNamespace(
            name="MOVE-ORIGINAL", legacy_entry_name="ENTRY-ORIGINAL", movement_date="2026-10-20", amount="10.25",
            currency="USD", company="LaraTik LLC", source_cash_box="BOX-A",
            destination_cash_box=None, direction="Out", category="Cash", money_flow="Other",
            variance_type=None, variance_amount=None, accounting_voucher_type=None,
            accounting_voucher=None, reversal_of=None, docstatus=0,
            workflow_state="Draft", migration_state="Verified",
            funding_source="Company Cash", owner=row.owner,
        ),
        SimpleNamespace(
            name="MOVE-REVERSAL", legacy_entry_name=row.name, movement_date="2026-10-20", amount="10.25",
            currency="USD", company="LaraTik LLC", source_cash_box="BOX-A",
            destination_cash_box=None, direction="Out", category="Cash", money_flow="Other",
            variance_type=None, variance_amount=None, accounting_voucher_type=None,
            accounting_voucher=None, reversal_of=None, docstatus=0,
            workflow_state="Approved", migration_state="Verified",
            funding_source="Company Cash", owner=row.owner,
        ),
    ])

    item = module.census()["rows"][0]

    assert item["target_content_matches_source"] is True
    assert item["target_lifecycle_matches_source"] is False
    assert item["target_matches_source"] is False


@pytest.mark.parametrize("docstatus, expected", [(0, None), (1, "MOVE-ORIGINAL")])
def test_only_submitted_original_mirrors_can_be_reversal_targets(migration, docstatus, expected):
    module, fake, _row, _targets = migration
    fake.db.get_value = lambda *args, **kwargs: SimpleNamespace(
        name="MOVE-ORIGINAL", docstatus=docstatus
    )

    assert module._submitted_mirror_for_legacy("ENTRY-ORIGINAL") == expected


def test_reversal_finalizer_waits_until_original_mirror_is_submitted(migration):
    module, fake, row, _targets = migration
    row.reversal_of = "ENTRY-ORIGINAL"
    row.status = "Reversed"
    committed = []

    def get_value(doctype, name, field=None, as_dict=False):
        if doctype != "LaraTik Cash Movement":
            return None
        if isinstance(name, dict):
            return "MOVE-REVERSAL" if name["legacy_entry_name"] == row.name else "MOVE-ORIGINAL"
        if name == "MOVE-ORIGINAL" and field == "docstatus":
            return 0
        raise AssertionError((doctype, name, field, as_dict))

    fake.db.get_value = get_value
    fake.db.commit = lambda: committed.append(True)
    fake.get_doc = lambda *args: (_ for _ in ()).throw(AssertionError("must not load reversal"))

    result = module.finalize_migrated_reversals()

    assert result == {"submitted_reversals": [], "writes_performed": False}
    assert committed == [True]


def test_existing_mirrors_are_loaded_as_one_bounded_page_lookup(migration):
    module, fake, row, targets = migration
    targets.append(SimpleNamespace(
        name="MOVE-1", legacy_entry_name=row.name, movement_date="2026-10-20", amount="10.25", currency="USD",
        company="LaraTik LLC", source_cash_box="BOX-A", destination_cash_box=None,
        direction="Out", category="Cash", money_flow="Other", variance_type=None,
        variance_amount=None, accounting_voucher_type=None, accounting_voucher=None,
        funding_source="Company Cash",
        reversal_of=None, docstatus=1, workflow_state="Submitted", migration_state="Verified",
        owner="cashier@example.invalid",
    ))

    result = module.census()

    assert result["already_mirrored"] == 1
    assert result["rows"][0]["target_matches_source"] is True


def test_reconciliation_compares_submitted_target_totals_to_source_totals(migration):
    module, _fake, row, targets = migration
    targets.append(SimpleNamespace(
        name="MOVE-1", legacy_entry_name=row.name, movement_date=row.entry_date,
        amount=row.amount, currency=row.currency, company="LaraTik LLC",
        source_cash_box="BOX-A", destination_cash_box=None, direction="Out",
        category="Cash", money_flow="Other", variance_type=None, variance_amount=None,
        accounting_voucher_type=None, accounting_voucher=None, funding_source="Company Cash",
        reversal_of=None, docstatus=1, workflow_state="Submitted", migration_state="Verified",
        owner=row.owner,
    ))

    result = module.reconciliation_report()

    assert result["submitted_mirror_rows"] == 1
    assert result["company_currency_totals"] == [{
        "company": "LaraTik LLC", "currency": "USD", "source_rows": 1,
        "source_amount": "10.25", "submitted_target_rows": 1,
        "submitted_target_amount": "10.25", "difference": "0.00",
    }]
    assert result["reconciliation_ready"] is True


def test_mirror_command_stops_before_writes_when_existing_target_content_differs(migration):
    module, _fake, row, targets = migration
    targets.append(SimpleNamespace(
        name="MOVE-1", legacy_entry_name=row.name, movement_date=row.entry_date,
        amount=row.amount, currency=row.currency, company="LaraTik LLC",
        source_cash_box="BOX-A", destination_cash_box=None, direction="Out",
        category="Cash", money_flow="Other", variance_type=None, variance_amount=None,
        accounting_voucher_type=None, accounting_voucher=None, funding_source="Other",
        reversal_of=None, docstatus=1, workflow_state="Submitted", migration_state="Verified",
        owner=row.owner,
    ))

    with pytest.raises(RuntimeError, match="existing mirrors differ from source rows"):
        module.mirror_legacy_movements(commit=True)


def test_mirror_lifecycle_mismatch_is_detected_even_when_amounts_match(migration):
    module, _fake, row, targets = migration
    targets.append(SimpleNamespace(
        name="MOVE-1", legacy_entry_name=row.name, movement_date="2026-10-20", amount="10.25", currency="USD",
        company="LaraTik LLC", source_cash_box="BOX-A", destination_cash_box=None,
        direction="Out", category="Cash", money_flow="Other", variance_type=None,
        variance_amount=None, accounting_voucher_type=None, accounting_voucher=None,
        funding_source="Company Cash",
        reversal_of=None, docstatus=0, workflow_state="Draft", migration_state="Verified",
        owner="cashier@example.invalid",
    ))

    result = module.census()

    assert result["rows"][0]["target_matches_source"] is False


def test_funding_source_mismatch_is_not_treated_as_a_reconciled_mirror(migration):
    module, _fake, row, targets = migration
    targets.append(SimpleNamespace(
        name="MOVE-1", legacy_entry_name=row.name, movement_date="2026-10-20", amount="10.25", currency="USD",
        company="LaraTik LLC", source_cash_box="BOX-A", destination_cash_box=None,
        direction="Out", category="Cash", money_flow="Other", variance_type=None,
        variance_amount=None, accounting_voucher_type=None, accounting_voucher=None,
        funding_source="Personal Cash", reversal_of=None, docstatus=1,
        workflow_state="Submitted", migration_state="Verified",
        owner="cashier@example.invalid",
    ))

    result = module.census()

    assert result["rows"][0]["target_matches_source"] is False


@pytest.mark.parametrize(
    "status, approval_status, expected",
    [
        ("Completed", "Approved", ("Request Approval", "Approve")),
        ("Pending", "Pending", ("Request Approval",)),
        ("Cancelled", "Not Required", ("Request Approval", "Reject")),
        ("Completed", "Rejected", ("Request Approval", "Reject")),
    ],
)
def test_legacy_approval_maps_through_real_frappe_workflow_transitions(
    migration, status, approval_status, expected
):
    module, _fake, _row, _targets = migration
    source = SimpleNamespace(status=status, approval_status=approval_status)

    assert module._historical_approval_actions(source) == expected


def test_reconciliation_report_aggregates_verified_totals_and_exposes_missing_mirrors(migration):
    module, _fake, _row, _targets = migration

    result = module.reconciliation_report()

    assert result["source_rows"] == 1
    assert result["missing_mirrors"] == 1
    assert result["reconciliation_ready"] is False
    assert result["company_currency_totals"] == [
        {"company": "LaraTik LLC", "currency": "USD", "source_rows": 1,
         "source_amount": "10.25", "submitted_target_rows": 0,
         "submitted_target_amount": "0", "difference": "-10.25"}
    ]
