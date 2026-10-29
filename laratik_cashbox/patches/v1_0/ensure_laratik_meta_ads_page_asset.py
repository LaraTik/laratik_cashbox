"""Verify the Meta Page asset child table after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_meta_ads_page_asset


def execute() -> None:
	laratik_meta_ads_page_asset()
