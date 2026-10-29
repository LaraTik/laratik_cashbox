"""Verify the dated Meta fee-rule schema after model sync."""

from laratik_cashbox.patches.v1_0.ensure_doctypes import laratik_meta_ads_fee_rule


def execute() -> None:
	laratik_meta_ads_fee_rule()
