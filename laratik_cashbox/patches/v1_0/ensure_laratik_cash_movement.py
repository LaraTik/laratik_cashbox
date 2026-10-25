"""Verify the standalone cash movement DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_cash_movement


def execute() -> None:
    laratik_cash_movement()
