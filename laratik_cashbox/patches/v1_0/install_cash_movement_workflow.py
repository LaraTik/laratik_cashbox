"""Install or verify the cash movement approval workflow after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import cash_movement_workflow


def execute() -> None:
    cash_movement_workflow()
