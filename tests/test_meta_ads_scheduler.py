"""Daily Meta Ads scheduling must retry month-close spend until it reconciles."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace


SCHEDULER_PATH = (
	Path(__file__).parents[1]
	/ "laratik_cashbox"
	/ "meta_ads_scheduler.py"
)


def load_scheduler(monkeypatch, runs):
	def get_all(_doctype, *, filters, fields, limit_page_length):
		assert fields == ["name"]
		assert limit_page_length == 1
		matches = []
		for run in runs:
			matched = True
			for field, expected in filters.items():
				value = getattr(run, field, None)
				if field == "from_date":
					matched = matched and value <= expected[1]
				elif field == "to_date":
					matched = matched and value >= expected[1]
				elif field == "fetched_at":
					matched = matched and value >= expected[1]
				else:
					matched = matched and value == expected
			if matched:
				matches.append(SimpleNamespace(name=run.name))
		return matches

	monkeypatch.setitem(sys.modules, "frappe", SimpleNamespace(get_all=get_all))
	spec = importlib.util.spec_from_file_location("meta_ads_scheduler_under_test", SCHEDULER_PATH)
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def sync_run(*, reconciliation_status, fetched_at="2026-11-04 00:00:00"):
	return SimpleNamespace(
		name="month-close-run",
		company="LaraTik",
		ad_account="account-a",
		status="Complete",
		ad_insights_status="Complete",
		ad_reconciliation_status=reconciliation_status,
		fetched_at=fetched_at,
		from_date="2026-10-01",
		to_date="2026-10-31",
	)


def test_mismatched_month_close_is_eligible_for_retry(monkeypatch):
	account = SimpleNamespace(company="LaraTik", name="account-a")
	scheduler = load_scheduler(monkeypatch, [sync_run(reconciliation_status="Mismatch")])

	assert not scheduler._has_complete_period_sync(
		account,
		date(2026, 10, 1),
		date(2026, 10, 31),
	)


def test_matched_month_close_is_considered_complete(monkeypatch):
	account = SimpleNamespace(company="LaraTik", name="account-a")
	scheduler = load_scheduler(monkeypatch, [sync_run(reconciliation_status="Matched")])

	assert scheduler._has_complete_period_sync(
		account,
		date(2026, 10, 1),
		date(2026, 10, 31),
	)


def test_month_close_snapshot_before_four_day_wait_is_stale(monkeypatch):
	account = SimpleNamespace(company="LaraTik", name="account-a")
	scheduler = load_scheduler(
		monkeypatch,
		[
			sync_run(
				reconciliation_status="Matched",
				fetched_at="2026-11-03 23:59:59",
			)
		],
	)

	assert not scheduler._has_complete_period_sync(
		account,
		date(2026, 10, 1),
		date(2026, 10, 31),
	)
