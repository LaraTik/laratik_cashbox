"""Queue daily Meta Ads Insights refreshes for active ad accounts."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe


ACCOUNT_PAGE_SIZE = 200
LOOKBACK_DAYS = 4
JOB_TIMEOUT_SECONDS = 1800
MONTH_CLOSE_START_DAY = 4


def schedule_daily_meta_ads_sync() -> dict[str, int]:
	"""Enqueue one account-local refresh per active Meta ad account.

	Each job re-reads the previous four complete account-local days so late
	Meta adjustments are captured. Paused accounts are not queued.
	"""
	logger = frappe.logger("meta_ads")
	queued = 0
	queued_month_close = 0
	skipped_timezone = 0
	start = 0

	while True:
		accounts = frappe.get_all(
			"LaraTik Meta Ads Account",
			filters={"status": "Active"},
			fields=["name", "company", "timezone"],
			order_by="name asc",
			limit_start=start,
			limit_page_length=ACCOUNT_PAGE_SIZE,
		)
		if not accounts:
			break

		for account in accounts:
			try:
				account_timezone = ZoneInfo(account.timezone or "")
			except (ZoneInfoNotFoundError, ValueError):
				skipped_timezone += 1
				logger.warning(
					"Skipping daily Meta Ads sync for account %s: missing or invalid time zone.",
					account.name,
				)
				continue

			local_today = datetime.now(timezone.utc).astimezone(account_timezone).date()
			until = local_today - timedelta(days=1)
			since = until - timedelta(days=LOOKBACK_DAYS - 1)
			_enqueue_account_sync(
				account,
				since,
				until,
				job_id=f"meta_ads_daily_{account.name}",
			)
			queued += 1

			if local_today.day >= MONTH_CLOSE_START_DAY:
				month_end = local_today.replace(day=1) - timedelta(days=1)
				month_start = month_end.replace(day=1)
				if not _has_complete_period_sync(account, month_start, month_end):
					_enqueue_account_sync(
						account,
						month_start,
						month_end,
						job_id=f"meta_ads_month_close_{account.name}_{month_start:%Y%m}",
					)
					queued_month_close += 1

		if len(accounts) < ACCOUNT_PAGE_SIZE:
			break
		start += len(accounts)

	return {
		"queued_daily": queued,
		"queued_month_close": queued_month_close,
		"skipped_timezone": skipped_timezone,
	}


def _enqueue_account_sync(account, since, until, job_id: str) -> None:
	frappe.enqueue(
		"laratik_cashbox.meta_ads_scheduler.sync_account_spend_period",
		queue="long",
		timeout=JOB_TIMEOUT_SECONDS,
		job_id=job_id,
		deduplicate=True,
		enqueue_after_commit=True,
		company=account.company,
		account_name=account.name,
		since=since.isoformat(),
		until=until.isoformat(),
	)


def _has_complete_period_sync(account, since, until) -> bool:
	runs = frappe.get_all(
		"LaraTik Meta Ads Sync",
		filters={
			"company": account.company,
			"ad_account": account.name,
			"status": "Complete",
			"ad_insights_status": "Complete",
			"from_date": ["<=", since.isoformat()],
			"to_date": [">=", until.isoformat()],
		},
		fields=["name"],
		limit_page_length=1,
	)
	return bool(runs)


def sync_account_spend_period(company: str, account_name: str, since: str, until: str):
	"""Run the shared, permission-checked Insights sync for one account."""
	from laratik_cashbox.meta_ads_api import sync_spend

	return sync_spend(
		company=company,
		account_name=account_name,
		since=since,
		until=until,
	)
