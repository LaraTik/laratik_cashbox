"""Meta invoice-summary CSV imports are validated, scoped evidence snapshots."""

from __future__ import annotations

import base64
import importlib
import sys
import types
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest


class FrappeError(Exception):
    pass


class FakeDoc:
    def __init__(self, frappe, **fields):
        self._frappe = frappe
        self.__dict__.update(fields)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def check_permission(self, _permission):
        if getattr(self, "deny_read", False):
            raise FrappeError("Permission denied")

    def as_dict(self):
        result = {}
        for key, value in vars(self).items():
            if key == "_frappe":
                continue
            if isinstance(value, list):
                result[key] = [vars(row) if hasattr(row, "__dict__") else row for row in value]
            else:
                result[key] = value
        return result

    def insert(self, ignore_permissions=False):
        del ignore_permissions
        if not getattr(self, "name", None):
            self.name = f"{getattr(self, 'doctype', 'Doc')}-{len(self._frappe.inserted) + 1}"
        self._frappe.inserted.append(self)
        if getattr(self, "doctype", None) == "LaraTik Meta Ads Payment":
            self._frappe.db.transactions[self.transaction_key] = self
        return self

    def db_set(self, field, value, update_modified=False):
        del update_modified
        setattr(self, field, value)


@pytest.fixture
def billing(monkeypatch):
    """Load the real billing modules against a small in-memory Frappe surface."""
    module_names = ("laratik_cashbox.meta_ads_billing", "laratik_cashbox.meta_ads_api")
    saved_modules = {name: sys.modules.get(name) for name in module_names}

    class FakeDB:
        def __init__(self):
            self.transactions = {}
            self.company_names = {"Company A", "Company B"}

        def exists(self, doctype, name):
            if doctype == "Company":
                return name in self.company_names
            if doctype in ("Currency", "Customer"):
                return bool(name)
            return True

        def get_value(self, doctype, name, field=None, as_dict=False):
            if doctype == "LaraTik Meta Ads Payment" and isinstance(name, dict):
                stored = self.transactions.get(name.get("transaction_key"))
                if not stored:
                    return None
                fields = field if isinstance(field, (list, tuple)) else []
                values = {key: getattr(stored, key, None) for key in fields}
                return SimpleNamespace(**values) if as_dict else values.get(field)
            if doctype == "Currency" and field == "enabled":
                return 1
            if doctype == "Customer" and field == "customer_name":
                return f"{name} display"
            return None

    fake = types.ModuleType("frappe")
    fake.db = FakeDB()
    fake.session = SimpleNamespace(user="Administrator")
    fake.inserted = []
    fake.saved_files = []
    fake.company_access_denied = set()
    fake.only_for = lambda _role: None
    fake.throw = lambda message: (_ for _ in ()).throw(FrappeError(str(message)))

    def profile_doc(name, *, company="Company A", customer="Example Customer", fee="5", currency="USD"):
        return FakeDoc(
            fake,
            doctype="LaraTik Meta Ads Billing Profile",
            name=name,
            company=company,
            customer=customer,
            relationship_kind="Customer",
            fee_percent=fee,
            fee_rules=[SimpleNamespace(effective_from="2026-10-01", fee_percent="12")],
            invoice_currency=currency,
            disabled=0,
            page_assets=[SimpleNamespace(
                page_id="900000000000001",
                page_name="Example Page",
                instagram_account_id="17841400000000001",
                instagram_username="example_ig",
                served_customer="Example Customer",
            )],
        )

    fake.account = FakeDoc(
        fake,
        doctype="LaraTik Meta Ads Account",
        name="Meta Account A",
        company="Company A",
        billing_profile="Example Profile",
        external_account_id="act_123",
        account_name="Example Ads",
        currency="EUR",
        check_permission=lambda _permission: None,
    )
    fake.profile = profile_doc("Example Profile")

    def get_doc(doctype, name=None):
        if isinstance(doctype, dict):
            return FakeDoc(fake, **deepcopy(doctype))
        if doctype == "Company":
            return FakeDoc(
                fake,
                doctype=doctype,
                name=name,
                check_permission=lambda _permission: (
                    (_ for _ in ()).throw(FrappeError("Company access denied"))
                    if name in fake.company_access_denied else None
                ),
            )
        if doctype == "LaraTik Meta Ads Account":
            return fake.account
        if doctype == "LaraTik Meta Ads Billing Profile":
            return fake.profile
        raise AssertionError(f"Unexpected get_doc({doctype!r}, {name!r})")

    fake.get_doc = get_doc
    fake.get_all = lambda *_args, **_kwargs: []
    fake.whitelist = lambda *args, **kwargs: lambda method: method

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda value: datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    utils.now_datetime = lambda: datetime(2026, 10, 28, 12, 0, 0)
    file_manager = types.ModuleType("frappe.utils.file_manager")

    def save_file(name, content, attached_to_doctype, attached_to_name, is_private):
        fake.saved_files.append((name, content, attached_to_doctype, attached_to_name, is_private))
        return FakeDoc(fake, name=f"FILE-{len(fake.saved_files)}")

    file_manager.save_file = save_file
    requests = types.ModuleType("requests")
    requests.RequestException = RuntimeError
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    monkeypatch.setitem(sys.modules, "frappe.utils.file_manager", file_manager)
    monkeypatch.setitem(sys.modules, "requests", requests)
    for name in module_names:
        sys.modules.pop(name, None)

    api = importlib.import_module("laratik_cashbox.meta_ads_api")
    module = importlib.import_module("laratik_cashbox.meta_ads_billing")
    yield module, fake

    for name in module_names:
        sys.modules.pop(name, None)
        if saved_modules[name] is not None:
            sys.modules[name] = saved_modules[name]
    del api


def csv_report(rows, total, *, account="act_123", currency="EUR"):
    lines = [
        f"Account: {account}",
        "Billing report: Oct 1, 2026 - Oct 31, 2026",
        "Payment method: Mastercard",
        "Date,Transaction ID,Amount,Currency",
    ]
    lines.extend(f"2026-10-02,{transaction_id},{amount},{currency}" for transaction_id, amount in rows)
    lines.append(f",Total amount billed,{total},{currency}")
    return "\n".join(lines).encode("utf-8")


def call_preview(module, csv_bytes):
    encoded = base64.b64encode(csv_bytes).decode("ascii")
    return module.preview_billing_report("Company A", "Meta Account A", "meta.csv", encoded)


def test_csv_parser_accepts_valid_account_and_checks_section_total(billing):
    module, _fake = billing
    parsed = module._parse_report(csv_report([("txn-1", "10.25")], "10.25"), _fake.account)

    assert parsed["currency"] == "EUR"
    assert parsed["start_date"] == "2026-10-02"
    assert parsed["end_date"] == "2026-10-02"
    assert parsed["counts"]["Payment"] == 1
    assert parsed["totals"]["Payment"] == "10.25"


def test_spend_report_lists_all_company_accounts_while_scoping_selected_report(billing, monkeypatch):
    _billing_module, fake = billing
    api = importlib.import_module("laratik_cashbox.meta_ads_api")
    just_halal = FakeDoc(
        fake,
        doctype="LaraTik Meta Ads Account",
        name="Test Meta Account",
        company="Company A",
        external_account_id="act_900000000000101",
        account_name="Test Meta Ad Account",
        currency="EUR",
        timezone="Europe/Berlin",
        billing_profile="",
        status="Active",
        last_sync_at=None,
        last_sync_status="",
    )
    fake.account.timezone = "Europe/Berlin"
    fake.account.status = "Active"
    fake.account.last_sync_at = None
    fake.account.last_sync_status = ""
    company_accounts = [fake.account, just_halal]
    original_get_doc = fake.get_doc

    def get_doc(doctype, name=None):
        if doctype == "LaraTik Meta Ads Account" and name == just_halal.name:
            return just_halal
        return original_get_doc(doctype, name)

    def get_all(doctype, *, filters=None, **_kwargs):
        if doctype == "LaraTik Meta Ads Account":
            if filters == {"company": "Company A"}:
                return company_accounts
            if filters == {"company": "Company A", "name": just_halal.name}:
                return [just_halal]
        return []

    monkeypatch.setattr(fake, "get_doc", get_doc)
    monkeypatch.setattr(fake, "get_all", get_all)

    result = api.get_spend_report(
        "Company A", "2026-10-01", "2026-10-27", account_name=just_halal.name
    )

    assert [account["name"] for account in result["account_options"]] == [
        "Meta Account A",
        "Test Meta Account",
    ]
    assert [account["name"] for account in result["accounts"]] == [just_halal.name]
    assert result["account_options_truncated"] is False


@pytest.mark.parametrize(
    ("csv_bytes", "message"),
    [
        (csv_report([("txn-1", "10.00")], "10.00", account="act_999"), "account ID does not match"),
        (csv_report([("txn-1", "10.00")], "9.00"), "do not match its section total"),
        (csv_report([("txn-1", "10.00")], "10.00", currency="USD"), "currency does not match"),
    ],
)
def test_csv_parser_rejects_account_currency_and_section_total_mismatches(billing, csv_bytes, message):
    module, fake = billing
    with pytest.raises(FrappeError, match=message):
        module._parse_report(csv_bytes, fake.account)


def test_exact_duplicate_transaction_ids_inside_csv_are_counted_once_for_import(billing):
    module, fake = billing
    result = call_preview(module, csv_report([("txn-1", "10.00"), ("txn-1", "10.00")], "10.00"))

    assert result["row_count"] == 2
    assert result["duplicate_count"] == 1
    assert result["new_row_count"] == 1
    assert result["payment_total"] == "10"
    assert result["conflict_count"] == 0
    assert result["can_import"] is True
    assert fake.inserted == []
    assert fake.saved_files == []


def test_conflicting_transaction_ids_inside_csv_block_the_import_without_writes(billing):
    module, fake = billing
    content = csv_report([("txn-1", "10.00"), ("txn-1", "11.00")], "21.00")

    preview = call_preview(module, content)
    assert preview["conflict_count"] == 1
    assert preview["conflicts"] == [{"row_number": 6, "code": "DUPLICATE_ID_DIFFERS"}]
    assert preview["can_import"] is False

    encoded = base64.b64encode(content).decode("ascii")
    with pytest.raises(FrappeError, match="conflicting transaction IDs"):
        module.import_billing_report("Company A", "Meta Account A", "meta.csv", encoded)
    assert fake.inserted == []
    assert fake.saved_files == []


def test_existing_exact_transaction_is_a_duplicate_but_changed_financial_details_conflict(billing):
    module, fake = billing
    existing = SimpleNamespace(
        name="PAY-1",
        transaction_date="2026-10-02",
        amount="10.00",
        currency="EUR",
        payment_method="Mastercard",
        record_kind="Payment",
    )
    transaction_key = module.hashlib.sha256(b"123:txn-1").hexdigest()
    fake.db.transactions[transaction_key] = existing

    exact = call_preview(module, csv_report([("txn-1", "10.00")], "10.00"))
    assert exact["duplicate_count"] == 1
    assert exact["new_row_count"] == 0
    assert exact["can_import"] is True

    conflict = call_preview(module, csv_report([("txn-1", "12.00")], "12.00"))
    assert conflict["duplicate_count"] == 0
    assert conflict["conflict_count"] == 1
    assert conflict["conflicts"] == [{"row_number": 5, "code": "TRANSACTION_DIFFERS"}]


def test_payment_rows_keep_import_time_payer_fee_currency_and_page_snapshots(billing):
    module, fake = billing
    content = csv_report([("txn-1", "10.00")], "10.00")
    encoded = base64.b64encode(content).decode("ascii")

    first = module.import_billing_report("Company A", "Meta Account A", "meta.csv", encoded)
    payment = next(row for row in fake.inserted if row.doctype == "LaraTik Meta Ads Payment")
    original_snapshot = {
        "customer": payment.billing_customer_snapshot,
        "relationship": payment.relationship_kind_snapshot,
        "fee": payment.fee_percent_snapshot,
        "invoice_currency": payment.invoice_currency_snapshot,
        "pages": payment.page_assets_snapshot,
    }
    assert original_snapshot == {
        "customer": "Example Customer",
        "relationship": "Customer",
        "fee": "12",
        "invoice_currency": "USD",
        "pages": '[{"instagram_account_id":"17841400000000001","instagram_username":"example_ig","page_id":"900000000000001","page_name":"Example Page","served_customer":"Example Customer"}]',
    }
    assert first["inserted_row_count"] == 1
    assert fake.saved_files[0][4] == 1

    fake.profile = FakeDoc(
        fake,
        doctype="LaraTik Meta Ads Billing Profile",
        name="Example Profile",
        company="Company A",
        customer="Different Payer",
        relationship_kind="Third Party",
        fee_percent="30",
        fee_rules=[],
        invoice_currency="EUR",
        disabled=0,
        page_assets=[SimpleNamespace(page_id="999", page_name="New Page", instagram_account_id="", instagram_username="", served_customer="")],
    )
    second = module.import_billing_report("Company A", "Meta Account A", "meta.csv", encoded)
    assert second["inserted_row_count"] == 0
    assert second["duplicate_row_count"] == 1
    assert {
        "customer": payment.billing_customer_snapshot,
        "relationship": payment.relationship_kind_snapshot,
        "fee": payment.fee_percent_snapshot,
        "invoice_currency": payment.invoice_currency_snapshot,
        "pages": payment.page_assets_snapshot,
    } == original_snapshot


def test_preview_refuses_cross_company_account_before_saving_file_or_rows(billing):
    module, fake = billing
    fake.account.company = "Company B"

    with pytest.raises(FrappeError, match="belongs to another Company"):
        call_preview(module, csv_report([("txn-1", "10.00")], "10.00"))

    assert fake.inserted == []
    assert fake.saved_files == []


def test_billing_api_requires_system_manager_before_parsing_or_writing(billing, monkeypatch):
    module, fake = billing
    calls = []
    fake.session.user = "finance-reader@example.test"
    fake.only_for = lambda role: calls.append(role) or (_ for _ in ()).throw(FrappeError("Not permitted"))
    monkeypatch.setattr(module, "_parse_report", lambda *_args: pytest.fail("CSV parsed before authorization"))

    with pytest.raises(FrappeError, match="Not permitted"):
        call_preview(module, csv_report([("txn-1", "10.00")], "10.00"))

    assert calls == ["System Manager"]
    assert fake.inserted == []
    assert fake.saved_files == []


def test_preview_refuses_a_company_the_integration_user_cannot_read(billing, monkeypatch):
    module, fake = billing
    fake.company_access_denied.add("Company B")
    monkeypatch.setattr(module, "_parse_report", lambda *_args: pytest.fail("CSV parsed before company authorization"))

    with pytest.raises(FrappeError, match="Company access denied"):
        module.preview_billing_report(
            "Company B",
            "Meta Account A",
            "meta.csv",
            base64.b64encode(b"not parsed").decode("ascii"),
        )

    assert fake.inserted == []
    assert fake.saved_files == []


def test_report_rejects_an_account_outside_the_requested_company(billing):
    module, fake = billing
    account_filters = []

    def get_all(doctype, **kwargs):
        if doctype == "LaraTik Meta Ads Account":
            account_filters.append(kwargs["filters"])
        return []

    fake.get_all = get_all

    with pytest.raises(FrappeError, match="belonging to this Company"):
        module.get_billing_report("Company A", "2026-10-01", "2026-10-31", account_name="Account B")

    assert account_filters == [{"company": "Company A"}]
    assert fake.inserted == []
    assert fake.saved_files == []


def test_billing_import_history_includes_the_saved_meta_account_id(billing):
    module, fake = billing
    import_fields = []

    def get_all(doctype, **kwargs):
        if doctype == "LaraTik Meta Ads Account":
            return [SimpleNamespace(
                name="Meta Account A",
                external_account_id="act_123",
                account_name="Example Ads",
                currency="EUR",
                billing_profile="Example Profile",
            )]
        if doctype == "LaraTik Meta Ads Payment":
            return []
        if doctype == "LaraTik Meta Ads Payment Import":
            import_fields.extend(kwargs["fields"])
            return [SimpleNamespace(
                name="Import-1",
                ad_account="Meta Account A",
                external_account_id="act_123",
                billing_customer_snapshot="Example Customer",
                report_start_date="2026-10-01",
                report_end_date="2026-10-31",
                currency="EUR",
                report_row_count=2,
                inserted_row_count=2,
                duplicate_row_count=0,
                source="Meta Ads billing CSV",
                source_file="private-file.csv",
                imported_at="2026-10-28 12:00:00",
            )]
        raise AssertionError(f"Unexpected get_all({doctype!r})")

    fake.get_all = get_all
    report = module.get_billing_report("Company A", "2026-10-01", "2026-10-31")

    assert "external_account_id" in import_fields
    assert report["imports"][0]["external_account_id"] == "act_123"
    assert report["imports_truncated"] is False


def test_billing_import_history_reports_when_the_batch_limit_is_reached(billing):
    module, fake = billing

    def get_all(doctype, **_kwargs):
        if doctype == "LaraTik Meta Ads Account":
            return [SimpleNamespace(
                name="Meta Account A",
                external_account_id="act_123",
                account_name="Example Ads",
                currency="EUR",
                billing_profile="Example Profile",
            )]
        if doctype == "LaraTik Meta Ads Payment":
            return []
        if doctype == "LaraTik Meta Ads Payment Import":
            return [SimpleNamespace(
                name=f"Import-{index}",
                ad_account="Meta Account A",
                external_account_id="act_123",
                billing_customer_snapshot="Example Customer",
                report_start_date="2026-10-01",
                report_end_date="2026-10-31",
                currency="EUR",
                report_row_count=2,
                inserted_row_count=2,
                duplicate_row_count=0,
                source="Meta Ads billing CSV",
                source_file=f"private-file-{index}.csv",
                imported_at=f"2026-10-{(index % 28) + 1:02d} 12:00:00",
            ) for index in range(101)]
        raise AssertionError(f"Unexpected get_all({doctype!r})")

    fake.get_all = get_all
    report = module.get_billing_report("Company A", "2026-10-01", "2026-10-31")

    assert report["imports_truncated"] is True
    assert len(report["imports"]) == 100
