"""The durable half of the idempotency story (ADR-174).

─── WHY THIS FILE EXISTS ─────────────────────────────────────────────────────

The application's at-most-once registry is PROCESS-LOCAL: a restart, or a
second instance, forgets every key. So a retry after a lost response — the
case ADR-112 made honest by answering ``ERP_OUTCOME_UNKNOWN`` — appends a
SECOND money row to the cash box's child table, and nothing notices.

The fix is to persist the key ON THE ROW and refuse a box that would carry it
twice. These tests are the receipt for the rule that decides.

They run without ERPNext: the rule is a pure function of the rows, and the
same reason ``rollup.py`` is pure is the reason this one is.

Run them with:

    cd laratik_cashbox && python3 -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from laratik_cashbox.idempotency import duplicate_idempotency_keys


def row(name: str = "", **overrides):
    """An entry row, with the fields this rule reads."""
    base = {"name": name, "idempotency_key": ""}
    base.update(overrides)
    return base


class TestNoDuplicate:
    def test_an_empty_box_has_no_duplicate(self):
        assert duplicate_idempotency_keys([]) == []

    def test_a_box_with_distinct_keys_has_no_duplicate(self):
        assert (
            duplicate_idempotency_keys(
                [row("A", idempotency_key="key-alpha"), row("B", idempotency_key="key-beta")]
            )
            == []
        )

    def test_a_key_may_appear_once(self):
        assert (
            duplicate_idempotency_keys([row("A", idempotency_key="key-alpha")]) == []
        )


class TestTheDuplicate:
    def test_a_repeated_key_is_reported(self):
        duplicates = duplicate_idempotency_keys(
            [row("A", idempotency_key="key-alpha"), row("B", idempotency_key="key-alpha")]
        )
        assert len(duplicates) == 1
        key, first = duplicates[0]
        assert key == "key-alpha"
        # The FIRST row is named, because a refusal that does not say which
        # row already holds the key leaves the operator to find it.
        assert first == "A"

    def test_a_third_repeat_is_reported_against_the_first(self):
        duplicates = duplicate_idempotency_keys(
            [
                row("A", idempotency_key="key-alpha"),
                row("B", idempotency_key="key-alpha"),
                row("C", idempotency_key="key-alpha"),
            ]
        )
        # The pair is reported once per REPEAT, always against the first row.
        assert [(key, first) for key, first in duplicates] == [
            ("key-alpha", "A"),
            ("key-alpha", "A"),
        ]

    def test_two_different_duplicates_are_both_reported(self):
        duplicates = duplicate_idempotency_keys(
            [
                row("A", idempotency_key="key-alpha"),
                row("B", idempotency_key="key-alpha"),
                row("C", idempotency_key="key-beta"),
                row("D", idempotency_key="key-beta"),
            ]
        )
        assert {key for key, _ in duplicates} == {"key-alpha", "key-beta"}


class TestRowsWithoutAKey:
    def test_a_row_with_no_key_is_not_a_duplicate(self):
        # Rows written before the field existed carry none, and a box full of
        # them is not a box with a thousand duplicate keys — refusing it would
        # make every existing box unmigratable.
        assert (
            duplicate_idempotency_keys(
                [row("A"), row("B"), row("C", idempotency_key=""), row("D", idempotency_key=None)]
            )
            == []
        )

    def test_a_keyed_row_beside_unkeyed_rows_still_reports_its_duplicate(self):
        duplicates = duplicate_idempotency_keys(
            [
                row("A"),
                row("B", idempotency_key="key-alpha"),
                row("C"),
                row("D", idempotency_key="key-alpha"),
            ]
        )
        assert duplicates == [("key-alpha", "B")]


class TestTheEdges:
    def test_a_row_with_no_name_yet_is_named_so_the_refusal_can_be_read(self):
        # A row that has never been saved has no name yet — and the refusal
        # still has to say something an operator can act on.
        duplicates = duplicate_idempotency_keys(
            [row("", idempotency_key="key-alpha"), row("", idempotency_key="key-alpha")]
        )
        assert len(duplicates) == 1
        assert duplicates[0][1] != ""

    def test_whitespace_around_a_key_is_not_a_second_key(self):
        # A key written with a trailing space is the same key, not a new one.
        duplicates = duplicate_idempotency_keys(
            [row("A", idempotency_key="key-alpha"), row("B", idempotency_key=" key-alpha ")]
        )
        assert len(duplicates) == 1

    def test_none_is_an_empty_box(self):
        assert duplicate_idempotency_keys(None) == []
