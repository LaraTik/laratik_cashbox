"""Verify the LaraTik Collection DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_collection


def execute() -> None:
    laratik_collection()
