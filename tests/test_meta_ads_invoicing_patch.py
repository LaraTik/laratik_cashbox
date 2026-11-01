"""The Meta invoice migration guard checks live Frappe metadata.

─── WHY THIS TEST EXISTS ─────────────────────────────────────────────────────────

A Select field's OPTIONS are not a field. `verify_doctype` compares the
fieldnames the repository declares against the fieldnames the site holds, and
it would happily pass a site whose `charge_model` Select still carries only
"Recharge" — the field is there, so the check is satisfied, and the site's
Select would silently drop the "Service fee only" value the application
writes.

That is the failure this patch exists to catch, and this test is what proves
the patch CATCHES it. A guard nobody has watched to fail is a comment.

So the stub below builds the site's metadata from the repository's own JSON,
which means the "healthy" case is the real schema — and each test then breaks
ONE thing and watches the patch refuse the migration.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


APP_ROOT = Path(__file__).parents[1] / "laratik_cashbox"
DOCTYPE_ROOT = APP_ROOT / "laratik_cashbox" / "doctype"
PATCH_PATH = APP_ROOT / "patches" / "v1_0" / "ensure_laratik_meta_ads_invoicing.py"


def _repo_fields(folder: str) -> dict:
	"""The fields the repository declares for one doctype, as the site would hold them."""
	with open(DOCTYPE_ROOT / folder / f"{folder}.json", encoding="utf-8") as handle:
		raw = json.load(handle)
	return {
		field["fieldname"]: SimpleNamespace(
			fieldname=field["fieldname"],
			fieldtype=field.get("fieldtype", "Data"),
			options=field.get("options", ""),
		)
		for field in raw.get("fields", [])
		if field.get("fieldname")
	}


def _site_meta(fields: dict):
	"""A Frappe meta stub: `has_field` and `get_field`, and nothing else."""

	def has_field(fieldname: str) -> bool:
		return fieldname in fields

	def get_field(fieldname: str):
		return fields.get(fieldname)

	return SimpleNamespace(has_field=has_field, get_field=get_field)


def load_patch(monkeypatch, site_fields: dict):
	"""Load the patch against a stubbed Frappe."""

	def throw(message):
		raise RuntimeError(message)

	frappe_stub = SimpleNamespace(
		db=SimpleNamespace(exists=lambda doctype, name: True),
		get_meta=lambda doctype: _site_meta(site_fields[doctype]),
		get_module_path=lambda module: str(APP_ROOT / module),
		throw=throw,
	)
	monkeypatch.setitem(sys.modules, "frappe", frappe_stub)

	# `ensure_doctypes` holds `frappe` as a module-level import, so a copy
	# cached by an earlier test would keep THAT test's stub and answer every
	# question from it. Drop it, so the patch is loaded against the stub this
	# test just installed.
	monkeypatch.delitem(
		sys.modules, "laratik_cashbox.patches.v1_0.ensure_doctypes", raising=False
	)

	spec = importlib.util.spec_from_file_location("meta_ads_invoicing_patch", PATCH_PATH)
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def healthy_site() -> dict:
	"""A site whose metadata matches the repository exactly."""
	return {
		"LaraTik Meta Ads Invoice Batch": _repo_fields("laratik_meta_ads_invoice_batch"),
		"LaraTik Meta Ads Invoice Line": _repo_fields("laratik_meta_ads_invoice_line"),
		"LaraTik Meta Ads Billing Profile": _repo_fields("laratik_meta_ads_billing_profile"),
		"LaraTik Meta Ads Spend": _repo_fields("laratik_meta_ads_spend"),
	}


def test_patch_accepts_the_schema_the_repository_declares(monkeypatch):
	"""The control: the current schema passes, so a failure below is the patch's teeth."""
	module = load_patch(monkeypatch, healthy_site())

	module.execute()


@pytest.mark.parametrize(
	("fieldname", "missing_value"),
	[
		("charge_model", "Service fee only"),
		("recognition_model", "Agent"),
		("tax_treatment", "Template"),
	],
)
def test_patch_rejects_a_select_that_cannot_hold_the_vocabulary(
	monkeypatch, fieldname, missing_value
):
	"""The failure this guard exists for.

	A Select field that cannot hold every value of its vocabulary would DROP
	that value on write — silently. The field is present, so a fieldname
	check passes it, and the policy decision the accountant made would vanish.
	"""
	site = healthy_site()
	profile = site["LaraTik Meta Ads Billing Profile"]
	kept = [option for option in profile[fieldname].options.split("\n") if option != missing_value]
	profile[fieldname].options = "\n".join(kept)

	module = load_patch(monkeypatch, site)

	with pytest.raises(RuntimeError, match=f"{fieldname} cannot hold '{missing_value}'"):
		module.execute()


def test_patch_rejects_a_missing_policy_field(monkeypatch):
	"""A field the repository declares and the site does not hold."""
	site = healthy_site()
	del site["LaraTik Meta Ads Billing Profile"]["fee_is_configured"]

	module = load_patch(monkeypatch, site)

	with pytest.raises(RuntimeError, match="fee_is_configured is missing"):
		module.execute()


def test_patch_rejects_a_missing_fee_status_snapshot(monkeypatch):
	"""The spend snapshot's fee status is what tells a zero fee from no fee."""
	site = healthy_site()
	del site["LaraTik Meta Ads Spend"]["fee_status_snapshot"]

	module = load_patch(monkeypatch, site)

	with pytest.raises(RuntimeError, match="fee_status_snapshot is missing"):
		module.execute()


def test_patch_rejects_an_invoice_line_that_did_not_sync(monkeypatch):
	"""The invoice state itself: no invoice line, no invoice."""
	site = healthy_site()
	site["LaraTik Meta Ads Invoice Line"].pop("charge_amount")

	module = load_patch(monkeypatch, site)

	with pytest.raises(RuntimeError, match="LaraTik Meta Ads Invoice Line"):
		module.execute()
