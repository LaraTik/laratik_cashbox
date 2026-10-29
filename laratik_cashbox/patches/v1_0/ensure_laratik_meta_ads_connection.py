"""Verify the encrypted Meta connection after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_meta_ads_connection


def execute() -> None:
	laratik_meta_ads_connection()
