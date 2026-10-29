"""Verify the daily Meta spend snapshot DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_meta_ads_spend


def execute() -> None:
	laratik_meta_ads_spend()
