"""Verify the immutable Meta payment ledger DocType after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_meta_ads_payment


def execute() -> None:
	laratik_meta_ads_payment()
