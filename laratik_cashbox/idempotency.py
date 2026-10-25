"""The DURABLE half of the idempotency story.

─── WHY THIS EXISTS ─────────────────────────────────────────────────────────

ADR-113's registry is PROCESS-LOCAL. A restart, or a second instance, forgets
every key — and the application module says so itself:

    It is **process-local**. A restart or a second instance forgets every key.

So a retry after a lost response — the case ADR-112 made honest by answering
``ERP_OUTCOME_UNKNOWN`` — appends a SECOND money row to the cash box's child
table, and nothing notices.

The architecture plan names the fix: *use persisted idempotency keys*. The key
goes ON THE ROW, and this module is the rule that reads it: a cash box may not
carry one key twice.

─── WHY IT IS A PURE MODULE ─────────────────────────────────────────────────

The same reason ``rollup.py`` and ``currency_policy.py`` are pure: the rule has
to be TESTABLE without a site. This one is a list of rows in and a list of
duplicates out, and ``laratik_cashbox/tests/test_idempotency.py`` exercises it
without ERPNext.

The controller turns a duplicate into a refusal; this module only says whether
there is one.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def duplicate_idempotency_keys(
    entries: Iterable[Mapping[str, Any]] | None,
) -> list[tuple[str, str]]:
    """The keys a box would carry TWICE, with the first row carrying each.

    Each entry is a ``(key, the name of the FIRST row that carried it)`` pair.
    The first row is named because a refusal that does not say WHICH row
    already holds the key leaves the operator to find it.

    **A row with no key carries none.** Rows written before the field existed
    have no key, and a box full of them is not a box with a thousand duplicate
    keys — refusing it would make every existing box unmigratable.
    """
    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []

    for row in entries or []:
        key = str(row.get("idempotency_key") or "").strip()
        if not key:
            continue
        name = str(row.get("name") or "").strip() or "(a row with no name yet)"
        if key in seen:
            duplicates.append((key, seen[key]))
            continue
        seen[key] = name

    return duplicates
