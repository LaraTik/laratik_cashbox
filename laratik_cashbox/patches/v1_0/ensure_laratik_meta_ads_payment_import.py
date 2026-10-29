"""Verify the Meta payment import batch DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_meta_ads_payment_import


def execute() -> None:
	laratik_meta_ads_payment_import()
