"""Verify the cash box DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_cash_box


def execute() -> None:
    laratik_cash_box()
