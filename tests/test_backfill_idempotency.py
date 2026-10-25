"""The backfill patch, run TWICE.

─── WHY THIS FILE EXISTS ─────────────────────────────────────────────────────

`bench migrate` runs every patch in `patches.txt`, and a patch that runs
twice must not do the work twice. That is easy to assert and easy to get
wrong: the backfill writes a box's BALANCE COLUMNS, and a second run that
recomputed and rewrote them would replace an operator's figure with a
different computation — on a column that is supposed to be filled only when
it is EMPTY.

Until now the property was asserted by READING the code: "it fills only
empty columns, so a second run finds nothing to fill". That is an argument,
not a receipt. This file is the receipt: the patch is executed twice against
a fake `frappe`, and the second run is watched to write NOTHING.

─── WHY A FAKE `frappe` AND NOT A SITE ────────────────────────────────────────

The patch imports `frappe` at module level, and ERPNext is not installed on
the machine that runs this suite. So the fake stands in for the three things
the patch touches — `get_all`, `get_doc` and `logger` — and nothing else.
The rollup it calls is PURE (`compute_rollup` imports no Frappe), so the
figure the patch writes is the real one, computed by the real code.

The identifiers are the other half of the claim: a migration that renames or
recreates a document loses history, and a backfill that renamed a box would
do it silently. So the test asserts the patch never writes a column it did
not compute — and `name` and `owner` are not columns it computes.

Run them with:

    cd laratik_cashbox && python3 -m pytest tests/ -q
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest

PATCH_MODULE = "laratik_cashbox.patches.v1_0.backfill_laratik_cash_box_balances"

#: The columns the patch fills. Mirrors `BACKFILL_FIELDS` — and is asserted
#: against it below, because a test that hard-codes the list would keep
#: passing if the patch changed one.
BACKFILL_FIELDS = (
    "current_balance",
    "total_cash",
    "total_received",
    "total_distributed",
    "total_outstanding",
    "last_activity",
)


class FakeDoc:
    """A `LaraTik Cash Box` document, with the columns the patch reads and writes."""

    def __init__(self, name: str, **columns: Any) -> None:
        self.name = name
        self.owner = "integrated@example.invalid"
        self._columns: dict[str, Any] = dict(columns)
        #: Every `db_set` call, so a test can see what the patch WROTE.
        self.writes: list[dict[str, Any]] = []

    def get(self, field: str) -> Any:
        return self._columns.get(field)

    def _box_currency(self) -> str | None:
        return self.get("currency")

    def db_set(self, values: dict[str, Any], update_modified: bool = True) -> None:
        self.writes.append(dict(values))
        self._columns.update(values)

    def snapshot(self) -> dict[str, Any]:
        """Everything about the document that a second run could change."""
        return {
            "name": self.name,
            "owner": self.owner,
            "columns": dict(self._columns),
            "writes": len(self.writes),
        }


class Row(dict):
    """A row as Frappe returns it: a mapping that also answers `row.name`.

    Frappe's `_dict` is a `dict` subclass whose attribute access reads the
    key, and the patch does `box.name` on the row `get_all` handed it. A plain
    `dict` would not answer that, and the fake would be testing a shape the
    patch never sees.
    """

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - mirrors `_dict`
            raise AttributeError(item) from exc


class FakeFrappe(types.ModuleType):
    """The three `frappe` calls the patch makes, and nothing else."""

    def __init__(self) -> None:
        super().__init__("frappe")
        self.boxes: list[FakeDoc] = []
        self.logged: list[tuple] = []

    def get_all(self, doctype: str, fields=None, limit=0, **kwargs):  # noqa: ANN001
        assert doctype == "LaraTik Cash Box", doctype
        assert fields == ["name"], (
            "child-table fields must be read from the loaded document, not selected "
            "as parent-table columns"
        )
        return [
            Row(
                {
                    # The document's NAME is its identity, not one of its
                    # columns — so it is read from the document itself and
                    # not from the column map.
                    "name": box.name,
                    **{
                        field: box.get(field)
                        for field in (fields or [])
                        if field != "name"
                    },
                }
            )
            for box in self.boxes
        ]

    def get_doc(self, doctype: str, name: str) -> FakeDoc:
        assert doctype == "LaraTik Cash Box", doctype
        for box in self.boxes:
            if box.name == name:
                return box
        raise AssertionError(f"no such document: {name}")

    def logger(self):  # noqa: ANN201
        fake = self

        class _Logger:
            def info(self, message, *args):  # noqa: ANN001
                fake.logged.append((message, args))

        return _Logger()


def entry(direction: str = "In", amount: float = 100.0, **kwargs: Any) -> dict[str, Any]:
    """An entry row, with the fields the rollup reads."""
    row = {
        "entry_date": "2026-10-01",
        "direction": direction,
        "amount": amount,
        "currency": "USD",
        "category": "Cash",
        "status": "Completed",
        "approval_status": "Not Required",
        "variance_type": "None",
    }
    row.update(kwargs)
    return row


def empty_box(name: str = "BOX-1", **overrides: Any) -> FakeDoc:
    """A cash box whose derived columns are all EMPTY — the case the patch exists for."""
    columns: dict[str, Any] = {
        "balance_rule": "Outstanding",
        "currency": "USD",
        "entries": [entry(direction="In", amount=100.0), entry(direction="Out", amount=30.0)],
        "pending_items": [],
        "current_balance": None,
        "total_cash": None,
        "total_received": None,
        "total_distributed": None,
        "total_outstanding": None,
        "last_activity": None,
    }
    columns.update(overrides)
    return FakeDoc(name, **columns)


@pytest.fixture
def patch():
    """The patch module, with `frappe` replaced by the fake."""
    fake = FakeFrappe()
    saved = sys.modules.get("frappe")
    sys.modules["frappe"] = fake
    try:
        module = importlib.import_module(PATCH_MODULE)
        importlib.reload(module)  # binds `frappe` to the fake, not to a real site
        yield module, fake
    finally:
        if saved is None:
            sys.modules.pop("frappe", None)
        else:
            sys.modules["frappe"] = saved


# ─── THE FIRST RUN ────────────────────────────────────────────────────────────


def test_the_first_run_fills_the_empty_columns(patch):
    module, fake = patch
    fake.boxes = [empty_box()]

    module.backfill_laratik_cash_box_balances()

    box = fake.boxes[0]
    assert box.get("total_received") == 100.0
    assert box.get("total_distributed") == 30.0
    assert box.get("total_cash") == 70.0
    # The box's rule says which figure `current_balance` publishes, and this
    # box's rule is `Outstanding` — so it publishes the OUTSTANDING figure,
    # which is zero because the box owes nobody. The cash figure is
    # `total_cash`, and the two are different facts (ADR-170).
    assert box.get("current_balance") == 0.0


def test_the_main_box_s_rule_decides_which_figure_it_publishes(patch):
    """The same box, under the cash rule, publishes the cash figure.

    This is the whole reason the backfill reads the box's `balance_rule`
    rather than assuming one: the instance's own Server Script knew which box
    was the MAIN cash box from its own configuration, and this app carries
    that as the box's rule.
    """
    module, fake = patch
    fake.boxes = [empty_box(balance_rule="Received minus distributed")]

    module.backfill_laratik_cash_box_balances()

    box = fake.boxes[0]
    assert box.get("current_balance") == 70.0
    assert box.get("total_cash") == 70.0


def test_it_fills_only_the_EMPTY_columns(patch):
    """A figure that is already there is the operator's, and stays theirs.

    The instance's own Server Script may have been maintaining these columns
    correctly, and a migration that replaced them with a different
    computation would change numbers nobody asked it to change.
    """
    module, fake = patch
    fake.boxes = [empty_box(total_received=999.0)]

    module.backfill_laratik_cash_box_balances()

    box = fake.boxes[0]
    assert box.get("total_received") == 999.0, "a populated column was overwritten"
    assert box.get("total_distributed") == 30.0, "the empty columns were not filled"


# ─── THE SECOND RUN — THE CLAIM ──────────────────────────────────────────────


def test_running_it_twice_writes_nothing_the_second_time(patch):
    module, fake = patch
    fake.boxes = [empty_box()]

    module.backfill_laratik_cash_box_balances()
    after_first = fake.boxes[0].snapshot()
    assert after_first["writes"] == 1, "the first run should have written once"

    module.backfill_laratik_cash_box_balances()
    after_second = fake.boxes[0].snapshot()

    assert after_second["writes"] == after_first["writes"], (
        "the second run wrote to the document again; a migration that runs twice must not "
        "do the work twice"
    )
    assert after_second["columns"] == after_first["columns"], (
        "the second run changed a column; the patch must fill only what is EMPTY"
    )


def test_running_it_twice_preserves_the_historical_identifiers(patch):
    """A migration that renames or recreates a document loses history.

    `name` and `owner` are not columns the patch computes, so it must never
    write them — and the document it touched must still be the same one.
    """
    module, fake = patch
    fake.boxes = [empty_box("BOX-ORIGINAL-NAME")]

    module.backfill_laratik_cash_box_balances()
    module.backfill_laratik_cash_box_balances()

    box = fake.boxes[0]
    assert box.name == "BOX-ORIGINAL-NAME"
    assert box.owner == "integrated@example.invalid"
    for written in box.writes:
        assert "name" not in written
        assert "owner" not in written


def test_it_never_writes_a_column_it_did_not_compute(patch):
    module, fake = patch
    fake.boxes = [empty_box()]

    module.backfill_laratik_cash_box_balances()

    for written in fake.boxes[0].writes:
        assert set(written) <= set(BACKFILL_FIELDS), (
            f"the patch wrote columns it does not compute: "
            f"{sorted(set(written) - set(BACKFILL_FIELDS))}"
        )


def test_the_columns_the_test_knows_are_the_columns_the_patch_fills(patch):
    """The list above is a copy — so it is held against the patch's own.

    A test that hard-codes a list keeps passing when the code changes. This
    one fails.
    """
    module, _fake = patch
    assert tuple(module.BACKFILL_FIELDS) == BACKFILL_FIELDS


def test_a_box_with_nothing_to_fill_is_left_completely_alone(patch):
    module, fake = patch
    populated = empty_box(
        current_balance=70.0,
        total_cash=70.0,
        total_received=100.0,
        total_distributed=30.0,
        total_outstanding=0.0,
        last_activity="2026-10-01",
    )
    fake.boxes = [populated]

    module.backfill_laratik_cash_box_balances()

    assert populated.writes == [], "a box with every column filled was written to anyway"
