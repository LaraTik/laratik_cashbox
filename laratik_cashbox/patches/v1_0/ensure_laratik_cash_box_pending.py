"""Verify the cash box pending-row DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_cash_box_pending


def execute() -> None:
    laratik_cash_box_pending()
