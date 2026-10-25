"""Verify the LaraTik Artifact DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_artifact


def execute() -> None:
    laratik_artifact()
