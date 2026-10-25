"""Verify the LaraTik Index Entry DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_index_entry


def execute() -> None:
    laratik_index_entry()
