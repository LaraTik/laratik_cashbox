"""The Frappe-side backstop for reversals — does it actually FIRE?

─── WHY THIS FILE EXISTS ──────────────────────────────────────────────────────

The pure rules are tested in `test_reversal.py`, and they are the rules. But a
rule that is never wired into a refusal is a function nobody calls, and a
backstop that never fires is a comment.

This file executes the CONTROLLER — `LaraTik Cash Box.validate()` — against a
fake `frappe`, and watches it refuse the three ways a box's reversals can fail
to add up:

  * two legs against one original (the correction applied twice),
  * a leg naming an original the box does not carry (a correction of a
    movement nobody can see),
  * a row marked `Reversed` with no leg against it (the mark without the
    justification).

And it watches the box with a PROPER reversal pair go through — because a
backstop that refuses everything is not a backstop, it is an outage.

─── WHY A FAKE `frappe` AND NOT A SITE ────────────────────────────────────────

The controller imports `frappe` at module level, and ERPNext is not installed
on the machine that runs this suite. So the fake stands in for the two things
the controller touches — `throw` and the `Document` base class — and nothing
else. Every rule it enforces is PURE, so the refusal it produces is the real
one, computed by the real code.

Run them with:

    cd laratik_cashbox && python3 -m pytest tests/ -q
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest

CONTROLLER_MODULE = (
    "laratik_cashbox.laratik_cashbox.doctype.laratik_cash_box.laratik_cash_box"
)
ROLE_MODULE = "laratik_cashbox.main_role"


class FakeFrappeThrow(Exception):
    """What `frappe.throw` raises — an error the save is refused with."""


class FakeDocument:
    """The base class Frappe's `Document` provides, reduced to what is used.

    A Frappe document is a mapping with `get`/`set`, and the controller reads
    its child table with `self.get("entries")`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._data: dict[str, Any] = dict(kwargs)

    def get(self, field: str, default: Any = None) -> Any:
        return self._data.get(field, default)

    def set(self, field: str, value: Any) -> None:
        self._data[field] = value


@pytest.fixture
def controller():
    """The controller module, with a fake `frappe` standing in for the site."""
    saved = sys.modules.get("frappe")
    saved_model = sys.modules.get("frappe.model")
    saved_document = sys.modules.get("frappe.model.document")
    saved_role = sys.modules.get(ROLE_MODULE)

    fake = types.ModuleType("frappe")

    def _throw(message: str, *args: Any, **kwargs: Any):
        raise FakeFrappeThrow(str(message))

    fake.throw = _throw  # type: ignore[attr-defined]
    fake.db = types.SimpleNamespace(
        get_value=lambda *args, **kwargs: None,
        sql=lambda *args, **kwargs: [],
    )
    fake.get_all = lambda *args, **kwargs: []

    fake_model = types.ModuleType("frappe.model")
    fake_document = types.ModuleType("frappe.model.document")
    fake_document.Document = FakeDocument  # type: ignore[attr-defined]
    fake_model.document = fake_document  # type: ignore[attr-defined]
    fake.model = fake_model  # type: ignore[attr-defined]

    sys.modules["frappe"] = fake
    sys.modules["frappe.model"] = fake_model
    sys.modules["frappe.model.document"] = fake_document

    try:
        # `laratik_cashbox.main_role` holds its own reference to `frappe`, so
        # it must be re-imported against THIS fake. A cached module would talk
        # to the previous test's site — and the test would pass for the wrong
        # reason.
        sys.modules.pop(ROLE_MODULE, None)
        module = importlib.import_module(CONTROLLER_MODULE)
        importlib.reload(module)
        yield module
    finally:
        for key, value in (
            ("frappe", saved),
            ("frappe.model", saved_model),
            ("frappe.model.document", saved_document),
            (ROLE_MODULE, saved_role),
        ):
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def row(name: str = "", **overrides: Any) -> dict[str, Any]:
    """An entry row, with the fields the controller's rules read."""
    base = {
        "name": name,
        "status": "Completed",
        "direction": "In",
        "amount": 100,
        "currency": "USD",
        "category": "Cash",
        "variance_type": "None",
        "other_box": "",
        "counterparty": "",
        "funding_source": "Company Cash",
        "money_flow": "From Client to Us",
        "reversal_of": "",
        "idempotency_key": "",
    }
    base.update(overrides)
    return base


def box(controller, entries: list[dict[str, Any]]):
    doc = controller.LaraTikCashBox()
    doc.set("entries", entries)
    return doc


class TestTheBackstopFires:
    def test_two_legs_against_one_original_are_refused(self, controller):
        doc = box(
            controller,
            [
                row("ROW-1", status="Reversed"),
                row("ROW-2", direction="Out", reversal_of="ROW-1"),
                row("ROW-3", direction="Out", reversal_of="ROW-1"),
            ],
        )
        with pytest.raises(FakeFrappeThrow) as exc:
            doc.validate()
        assert "already reversed" in str(exc.value)

    def test_a_leg_naming_an_absent_original_is_refused(self, controller):
        doc = box(controller, [row("ROW-2", direction="Out", reversal_of="ROW-NOT-HERE")])
        with pytest.raises(FakeFrappeThrow) as exc:
            doc.validate()
        assert "does not carry" in str(exc.value)

    def test_a_reversed_row_with_no_leg_is_refused(self, controller):
        doc = box(controller, [row("ROW-1", status="Reversed")])
        with pytest.raises(FakeFrappeThrow) as exc:
            doc.validate()
        assert "nothing reverses it" in str(exc.value)


class TestTheBackstopDoesNotRefuseGoodBoxes:
    def test_a_proper_reversal_pair_is_accepted(self, controller):
        doc = box(
            controller,
            [
                row("ROW-1", status="Reversed"),
                row("ROW-2", direction="Out", reversal_of="ROW-1"),
            ],
        )
        doc.validate()  # must not raise

    def test_a_box_with_no_reversals_is_accepted(self, controller):
        doc = box(controller, [row("ROW-1"), row("ROW-2", direction="Out")])
        doc.validate()  # must not raise

    def test_the_idempotency_backstop_still_fires(self, controller):
        # The reversal backstop is ADDED to the guard that was already here.
        # If it had replaced it, a duplicate key would sail through.
        doc = box(
            controller,
            [
                row("ROW-1", idempotency_key="key-one"),
                row("ROW-2", direction="Out", idempotency_key="key-one"),
            ],
        )
        with pytest.raises(FakeFrappeThrow) as exc:
            doc.validate()
        assert "idempotency key" in str(exc.value)


class TestMainBalanceRoleBackstop:
    def main_box(self, controller, name="BOX-1", account="Cash - EX", rule=None):
        doc = controller.LaraTikCashBox()
        doc.name = name
        doc.set("cash_account", account)
        doc.set("balance_role", "Main")
        doc.set("balance_rule", rule or controller.RULE_RECEIVED_MINUS_DISTRIBUTED)
        return doc

    def declare(self, controller, rows, reads=None):
        """Stand in for the site's CURRENT read of the declared Main boxes.

        The uniqueness check is a LOCKING read (`FOR UPDATE`) — see
        `laratik_cashbox/main_role.py` — so the stub answers the query, and
        records it, rather than pretending a plain read happened.
        """
        def sql(query, values=None, **kwargs):
            if reads is not None:
                reads.append(query)
            if "balance_role" in query:
                return list(rows)
            return []

        controller.frappe.db.sql = sql

    def test_a_second_main_box_in_the_same_company_is_refused(self, controller):
        accounts = {"Cash - EX": "Example Ltd", "Bank - EX": "Example Ltd"}

        def get_value(doctype, name, fields, **kwargs):
            company = accounts.get(name) if doctype == "Account" else None
            return {"company": company} if company and kwargs.get("as_dict") else company

        controller.frappe.db.get_value = get_value
        self.declare(controller, [{"name": "BOX-OLD", "cash_account": "Bank - EX"}])

        with pytest.raises(FakeFrappeThrow, match="already has Main cash box"):
            self.main_box(controller).validate()

    def test_the_uniqueness_read_is_a_LOCKING_read(self, controller):
        """A plain read cannot see the declaration we are queued behind.

        Under REPEATABLE READ a plain SELECT reads the snapshot taken before
        the Company lock, so a Main box committed by the declaration ahead of
        us is invisible and BOTH pass. Only a CURRENT read sees it.
        """
        accounts = {"Cash - EX": "Example Ltd"}

        def get_value(doctype, name, fields, **kwargs):
            company = accounts.get(name) if doctype == "Account" else None
            return {"company": company} if company and kwargs.get("as_dict") else company

        controller.frappe.db.get_value = get_value
        reads: list[str] = []
        self.declare(controller, [], reads=reads)

        self.main_box(controller).validate()

        uniqueness_reads = [q for q in reads if "balance_role" in q]
        assert len(uniqueness_reads) == 1, "the uniqueness check must happen exactly once"
        assert "FOR UPDATE" in uniqueness_reads[0].upper(), (
            "the uniqueness read must be a CURRENT (locking) read, not a plain one"
        )

    def test_a_main_box_in_another_company_does_not_conflict(self, controller):
        accounts = {"Cash - EX": "Example Ltd", "Cash - OT": "Other Ltd"}

        def get_value(doctype, name, fields, **kwargs):
            company = accounts.get(name) if doctype == "Account" else None
            return {"company": company} if company and kwargs.get("as_dict") else company

        controller.frappe.db.get_value = get_value
        self.declare(controller, [{"name": "BOX-OTHER", "cash_account": "Cash - OT"}])

        self.main_box(controller).validate()

    def test_main_box_with_an_obligations_rule_is_refused(self, controller):
        controller.frappe.db.get_value = lambda *args, **kwargs: {"company": "Example Ltd"}

        with pytest.raises(FakeFrappeThrow, match="must use the 'Received minus distributed'"):
            self.main_box(controller, rule="Outstanding").validate()
