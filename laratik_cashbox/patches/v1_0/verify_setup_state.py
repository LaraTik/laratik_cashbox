"""Fail migration if the resumable setup-state schema did not sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_setup_state


def execute() -> None:
    laratik_setup_state()
