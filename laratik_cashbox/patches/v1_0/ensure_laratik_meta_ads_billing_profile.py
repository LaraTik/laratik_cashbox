"""Verify the Meta billing profile after schema sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_meta_ads_billing_profile


def execute() -> None:
	laratik_meta_ads_billing_profile()
