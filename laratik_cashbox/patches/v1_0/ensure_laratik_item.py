"""Verify the LaraTik Item DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_item


def execute() -> None:
    laratik_item()
