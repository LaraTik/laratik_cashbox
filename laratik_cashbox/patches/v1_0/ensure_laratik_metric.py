"""Verify the LaraTik Metric DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_metric


def execute() -> None:
    laratik_metric()
