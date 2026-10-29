"""The install patch must install and verify the intended Frappe Workflow."""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.patches.v1_0.ensure_doctypes"


class FakeFrappeThrow(Exception):
    pass


@pytest.fixture
def patch_module(monkeypatch):
    fake = types.ModuleType("frappe")
    fake.db = SimpleNamespace(exists=lambda doctype, name: False)
    fake.throw = lambda message: (_ for _ in ()).throw(FakeFrappeThrow(str(message)))
    fake.get_meta = lambda doctype: SimpleNamespace(
        get_field=lambda fieldname: SimpleNamespace(fieldtype="Link", options="Workflow State")
    )
    inserted = []

    class NewDoc:
        def __init__(self, values):
            self.values = values

        def insert(self, **kwargs):
            inserted.append(self.values)

    fake.get_doc = lambda value: NewDoc(value) if isinstance(value, dict) else None
    monkeypatch.setitem(sys.modules, "frappe", fake)
    sys.modules.pop(MODULE, None)
    module = importlib.import_module(MODULE)
    yield module, fake, inserted
    sys.modules.pop(MODULE, None)


def test_installs_workflow_with_separated_approval_and_completion(patch_module):
    module, _fake, inserted = patch_module

    module.cash_movement_workflow()

    workflow = inserted[-1]
    assert workflow["document_type"] == "LaraTik Cash Movement"
    state_docs = [row for row in inserted if row.get("doctype") == "Workflow State"]
    assert {row["workflow_state_name"] for row in state_docs} == {
        "Draft", "Pending Approval", "Approved", "Rejected", "Submitted"
    }
    action_docs = [row for row in inserted if row.get("doctype") == "Workflow Action Master"]
    assert {row["workflow_action_name"] for row in action_docs} == {
        "Request Approval", "Approve", "Reject", "Return for Changes", "Submit"
    }
    transitions = workflow["transitions"]
    assert next(row for row in transitions if row["action"] == "Approve")["allow_self_approval"] == 0
    assert not any(row["state"] == "Draft" and row["action"] == "Approve" for row in transitions)
    assert next(row for row in transitions if row["action"] == "Reject")["allow_self_approval"] == 0
    assert next(row for row in transitions if row["action"] == "Request Approval")["allow_self_approval"] == 1
    assert next(row for row in transitions if row["action"] == "Submit")["allow_self_approval"] == 1
    assert sum(row["action"] == "Return for Changes" for row in transitions) == 3
    assert next(row for row in workflow["states"] if row["state"] == "Submitted")["doc_status"] == "1"


def test_fresh_install_hook_creates_the_workflow(patch_module, monkeypatch):
    module, _fake, inserted = patch_module

    # This test isolates the movement workflow behavior. Fresh-install schema
    # verification for the independently tested Meta Ads doctypes is stubbed
    # so their presence does not mask whether the hook creates the workflow.
    for name in (
        "laratik_meta_ads_page_asset",
            "laratik_meta_ads_fee_rule",
        "laratik_meta_ads_connection",
        "laratik_meta_ads_billing_profile",
        "laratik_meta_ads_account",
        "laratik_meta_ads_spend",
        "laratik_meta_ads_sync",
        "laratik_meta_ads_payment_import",
        "laratik_meta_ads_payment",
    ):
        monkeypatch.setattr(module, name, lambda: None)

    module.after_install()

    assert inserted[-1]["doctype"] == "Workflow"
    assert inserted[-1]["workflow_name"] == "LaraTik Cash Movement Approval"


def test_workflow_state_uses_frappe_v16_link_field():
    import json
    from pathlib import Path

    schema_path = (
        Path(__file__).parents[1]
        / "laratik_cashbox"
        / "laratik_cashbox"
        / "doctype"
        / "laratik_cash_movement"
        / "laratik_cash_movement.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    state_field = next(field for field in schema["fields"] if field["fieldname"] == "workflow_state")

    assert state_field["fieldtype"] == "Link"
    assert state_field["options"] == "Workflow State"
    assert state_field["hidden"] == 1
    assert state_field["allow_on_submit"] == 1
    assert state_field["no_copy"] == 1


def test_rejects_schema_with_nonstandard_workflow_state_field(patch_module):
    module, fake, _inserted = patch_module
    fake.get_meta = lambda doctype: SimpleNamespace(
        get_field=lambda fieldname: SimpleNamespace(fieldtype="Data", options=None)
    )

    with pytest.raises(FakeFrappeThrow, match="must be a Link to Workflow State"):
        module.cash_movement_workflow()


def test_existing_incomplete_workflow_is_rejected(patch_module):
    module, fake, _inserted = patch_module
    fake.db.exists = lambda doctype, name: True
    fake.get_doc = lambda *args: SimpleNamespace(
        document_type="LaraTik Cash Movement",
        is_active=1,
        workflow_state_field="workflow_state",
        states=[],
        transitions=[],
    )

    with pytest.raises(FakeFrappeThrow, match="does not match the required state machine"):
        module.cash_movement_workflow()
