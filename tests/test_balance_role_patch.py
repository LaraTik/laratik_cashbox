"""The balance-role migration refuses to guess and fills only blank roles."""

import importlib
import sys
import types

import pytest


MODULE = "laratik_cashbox.patches.v1_0.backfill_cash_box_balance_role"


@pytest.fixture
def role_patch(monkeypatch):
    fake = types.ModuleType("frappe")
    fake.calls = []
    fake.rows = []
    fake.logs = []
    fake.blanks = 0
    fake.meta = types.SimpleNamespace(get_field=lambda name: object() if name == "balance_role" else None)
    fake.get_meta = lambda doctype: fake.meta
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.logger = lambda: types.SimpleNamespace(info=lambda *args: fake.logs.append(args))

    def sql(query, values=None, as_dict=False):
        fake.calls.append((query, values, as_dict))
        if "COUNT(*)" in query:
            return [{"blanks": fake.blanks}]
        if query.lstrip().startswith("SELECT"):
            return fake.rows
        return None

    fake.db = types.SimpleNamespace(sql=sql)
    monkeypatch.setitem(sys.modules, "frappe", fake)
    sys.modules.pop(MODULE, None)
    module = importlib.import_module(MODULE)
    yield module, fake
    sys.modules.pop(MODULE, None)


def test_patch_fills_only_missing_roles_as_custody(role_patch):
    module, fake = role_patch
    fake.blanks = 3

    module.execute()

    updates = [call for call in fake.calls if call[0].lstrip().startswith("UPDATE")]
    assert len(updates) == 1
    assert updates[0][1] == ("Custody",)
    assert "balance_role IS NULL OR TRIM(balance_role) = ''" in updates[0][0]


def test_patch_says_what_it_did_and_never_promotes_a_row(role_patch):
    """A migration that changes rows and says nothing cannot be audited."""
    module, fake = role_patch
    fake.blanks = 3

    module.execute()

    assert len(fake.logs) == 1
    message, count = fake.logs[0]
    assert count == 3
    assert "%s" in message
    assert "no box was promoted to Main" in message
    # And the write itself never promotes: it only fills blanks with Custody.
    writes = " ".join(call[0] for call in fake.calls if call[0].lstrip().startswith("UPDATE"))
    assert "'Main'" not in writes


def test_patch_refuses_unknown_role_instead_of_overwriting_it(role_patch):
    module, fake = role_patch
    fake.rows = [{"name": "BOX-1", "balance_role": "Primary"}]

    with pytest.raises(ValueError, match="unsupported balance_role"):
        module.execute()

    assert len(fake.calls) == 1
