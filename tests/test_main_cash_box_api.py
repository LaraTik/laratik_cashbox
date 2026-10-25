"""The Main cash box setup method changes exactly one Company's role atomically."""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


MODULE = "laratik_cashbox.api"
ROLE_MODULE = "laratik_cashbox.main_role"


class FakeDoc:
    def __init__(self, name, **values):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "permissions", [])
        object.__setattr__(self, "saved", 0)
        object.__setattr__(self, "denied", set())

    def __getattr__(self, key):
        values = object.__getattribute__(self, "values")
        if key in values:
            return values[key]
        raise AttributeError(key)

    def __setattr__(self, key, value):
        if key in {"name", "values", "permissions", "saved", "denied"}:
            object.__setattr__(self, key, value)
            return
        self.values[key] = value

    def get(self, key, default=None):
        return self.values.get(key, default)

    def check_permission(self, permission):
        self.permissions.append(permission)
        # A principal that may not do this is REFUSED. A fake that records the
        # permission and then says yes is a fake that cannot see the rule.
        if permission in self.denied:
            raise PermissionError(f"{self.name} may not {permission}")

    def save(self):
        self.saved += 1


@pytest.fixture
def main_cash_box_api(monkeypatch):
    fake = types.ModuleType("frappe")
    fake.session = SimpleNamespace(user="setup@laratik.local")
    fake.roles = {"System Manager"}
    fake.whitelist = lambda *args, **kwargs: lambda method: method
    fake.only_for = lambda role: (
        None if role in fake.roles else (_ for _ in ()).throw(PermissionError(role))
    )
    fake.throw = lambda message: (_ for _ in ()).throw(ValueError(str(message)))
    fake.companies = {"Example Ltd": FakeDoc("Example Ltd")}
    fake.accounts = {
        "Cash - EX": "Example Ltd",
        "Bank - EX": "Example Ltd",
        "Cash - OT": "Other Ltd",
    }
    fake.docs = {
        ("LaraTik Cash Box", "BOX-OLD"): FakeDoc(
            "BOX-OLD",
            cash_account="Bank - EX",
            balance_rule="Received minus distributed",
            balance_role="Main",
        ),
        ("LaraTik Cash Box", "BOX-NEW"): FakeDoc(
            "BOX-NEW",
            cash_account="Cash - EX",
            balance_rule="Received minus distributed",
            balance_role="Custody",
        ),
        ("LaraTik Cash Box", "BOX-OTHER"): FakeDoc(
            "BOX-OTHER",
            cash_account="Cash - OT",
            balance_rule="Received minus distributed",
            balance_role="Main",
        ),
    }
    fake.locks = []
    fake.denied = set()

    def sql(query, values=None, **kwargs):
        """The site answers a query with rows; only the locking read has any."""
        fake.locks.append((query, values))
        if "tabLaraTik Cash Box`" in query and "balance_role" in query:
            return [
                {"name": name, "cash_account": doc.get("cash_account")}
                for (row_doctype, name), doc in fake.docs.items()
                if row_doctype == "LaraTik Cash Box" and doc.get("balance_role") == "Main"
            ]
        return []

    fake.db = SimpleNamespace(
        sql=sql,
        get_value=lambda doctype, name, field: fake.accounts.get(name) if doctype == "Account" else None,
    )

    def get_doc(doctype, name):
        if doctype == "Company":
            return fake.companies[name]
        if doctype == "LaraTik Cash Box":
            return fake.docs[(doctype, name)]
        if doctype == "Account":
            return FakeDoc(name, company=fake.accounts[name])
        raise AssertionError(f"unexpected DocType: {doctype}")

    fake.get_doc = get_doc
    fake.get_all = lambda doctype, **kwargs: [
        {"name": name, "cash_account": doc.get("cash_account")}
        for (row_doctype, name), doc in fake.docs.items()
        if row_doctype == doctype and doc.get("balance_role") == "Main"
    ]

    monkeypatch.setitem(sys.modules, "frappe", fake)
    sys.modules.pop(MODULE, None)
    # `laratik_cashbox.main_role` holds its own reference to `frappe`, so it
    # must be re-imported against THIS fake. A cached module would talk to the
    # previous test's site — and the test would pass for the wrong reason.
    sys.modules.pop(ROLE_MODULE, None)
    api = importlib.import_module(MODULE)
    yield api, fake
    sys.modules.pop(MODULE, None)
    sys.modules.pop(ROLE_MODULE, None)


def test_switch_demotes_only_the_same_company_main(main_cash_box_api):
    api, fake = main_cash_box_api

    result = api.set_main_cash_box(" Example Ltd ", "BOX-NEW")

    assert result == {"company": "Example Ltd", "cash_box": "BOX-NEW"}
    assert fake.docs[("LaraTik Cash Box", "BOX-OLD")].get("balance_role") == "Custody"
    assert fake.docs[("LaraTik Cash Box", "BOX-NEW")].get("balance_role") == "Main"
    assert fake.docs[("LaraTik Cash Box", "BOX-OTHER")].get("balance_role") == "Main"
    assert len(fake.locks) == 3
    assert fake.locks[0][1] == ("Example Ltd",)
    assert fake.locks[1][1] == ("BOX-NEW",)


def test_the_uniqueness_read_is_a_LOCKING_read(main_cash_box_api):
    """The check that "at most one Main per company" holds must SEE the other
    declaration.

    A plain SELECT under REPEATABLE READ reads the snapshot this transaction
    took before it waited for the Company lock — so a declaration that
    committed while we were queued is invisible, and BOTH pass. Only a
    CURRENT read (`FOR UPDATE`) sees it.

    So this test asserts the SQL itself. If someone swaps the locking read
    back for a plain one, this goes red — which is the point.
    """
    api, fake = main_cash_box_api

    api.set_main_cash_box("Example Ltd", "BOX-NEW")

    uniqueness_reads = [
        query for query, _ in fake.locks
        if "tabLaraTik Cash Box`" in query and "balance_role" in query
    ]
    assert len(uniqueness_reads) == 1, "the uniqueness check must happen exactly once"
    assert "FOR UPDATE" in uniqueness_reads[0].upper(), (
        "the uniqueness read must be a CURRENT (locking) read, not a plain one"
    )


def test_switch_is_refused_for_a_principal_that_may_not_write(main_cash_box_api):
    """A permission that is checked but never enforced is not a permission.

    The fake refuses `write` when the box is marked as denying it, and the
    method must let that refusal stand: nothing is saved, and the previous
    Main box stays Main.
    """
    api, fake = main_cash_box_api
    fake.docs[("LaraTik Cash Box", "BOX-NEW")].denied.add("write")

    with pytest.raises(PermissionError):
        api.set_main_cash_box("Example Ltd", "BOX-NEW")

    assert fake.docs[("LaraTik Cash Box", "BOX-NEW")].saved == 0
    assert fake.docs[("LaraTik Cash Box", "BOX-OLD")].get("balance_role") == "Main"
    assert fake.docs[("LaraTik Cash Box", "BOX-NEW")].get("balance_role") == "Custody"


def test_switch_refuses_a_cash_box_owned_by_another_company(main_cash_box_api):
    api, fake = main_cash_box_api

    with pytest.raises(ValueError, match="does not belong to this company"):
        api.set_main_cash_box("Example Ltd", "BOX-OTHER")

    assert fake.docs[("LaraTik Cash Box", "BOX-OLD")].get("balance_role") == "Main"
    assert fake.docs[("LaraTik Cash Box", "BOX-OTHER")].saved == 0


def test_switch_refuses_an_obligations_balance_rule(main_cash_box_api):
    api, fake = main_cash_box_api
    fake.docs[("LaraTik Cash Box", "BOX-NEW")].values["balance_rule"] = "Outstanding"

    with pytest.raises(ValueError, match="Received minus distributed"):
        api.set_main_cash_box("Example Ltd", "BOX-NEW")

    assert fake.docs[("LaraTik Cash Box", "BOX-OLD")].get("balance_role") == "Main"


def test_switch_requires_system_manager(main_cash_box_api):
    api, fake = main_cash_box_api
    fake.session.user = "viewer@laratik.local"
    fake.roles = set()

    with pytest.raises(PermissionError, match="System Manager"):
        api.set_main_cash_box("Example Ltd", "BOX-NEW")

    assert fake.locks == []
