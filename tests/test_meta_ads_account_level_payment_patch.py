"""The account-level Meta payment migration guard checks live Frappe metadata."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PATCH_PATH = (
	Path(__file__).parents[1]
	/ "laratik_cashbox"
	/ "patches"
	/ "v1_0"
	/ "ensure_laratik_meta_ads_account_level_payments.py"
)


def load_patch(monkeypatch, fields):
	def throw(message):
		raise RuntimeError(message)

	frappe_stub = SimpleNamespace(
		get_meta=lambda doctype: SimpleNamespace(
			get_field=lambda fieldname: fields.get(doctype, {}).get(fieldname)
		),
		throw=throw,
	)
	monkeypatch.setitem(sys.modules, "frappe", frappe_stub)
	spec = importlib.util.spec_from_file_location("account_level_payment_patch", PATCH_PATH)
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def valid_optional_fields():
	fields = {}
	for doctype in ("LaraTik Meta Ads Payment", "LaraTik Meta Ads Payment Import"):
		fields[doctype] = {
			fieldname: SimpleNamespace(reqd=0)
			for fieldname in (
				"billing_profile_snapshot",
				"billing_customer_snapshot",
				"relationship_kind_snapshot",
				"fee_percent_snapshot",
				"invoice_currency_snapshot",
			)
		}
	return fields


def test_patch_accepts_optional_account_level_snapshot_fields(monkeypatch):
	module = load_patch(monkeypatch, valid_optional_fields())

	module.execute()


def test_patch_rejects_a_snapshot_field_that_still_requires_a_payer(monkeypatch):
	fields = valid_optional_fields()
	fields["LaraTik Meta Ads Payment"]["billing_profile_snapshot"].reqd = 1
	module = load_patch(monkeypatch, fields)

	with pytest.raises(RuntimeError, match="billing_profile_snapshot is still required"):
		module.execute()


def test_patch_rejects_a_missing_snapshot_field(monkeypatch):
	fields = valid_optional_fields()
	del fields["LaraTik Meta Ads Payment Import"]["billing_customer_snapshot"]
	module = load_patch(monkeypatch, fields)

	with pytest.raises(RuntimeError, match="billing_customer_snapshot is missing"):
		module.execute()
