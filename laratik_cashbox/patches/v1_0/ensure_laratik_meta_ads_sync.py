"""Verify the Meta spend sync audit DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_meta_ads_sync


def execute() -> None:
	laratik_meta_ads_sync()
