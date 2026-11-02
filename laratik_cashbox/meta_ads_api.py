"""Permission-checked Meta Ads account discovery and daily spend snapshots.

The Meta Marketing API Insights endpoint supplies delivery spend, not proof of
an actual card charge. This module deliberately stores that source label with
each daily row and never calls a private Ads Manager billing endpoint.
"""

import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import frappe
import requests
from frappe.utils import getdate, now_datetime

from laratik_cashbox import meta_ads_chargeability as chargeability
from laratik_cashbox import meta_ads_invoicing as invoicing


META_TIMEOUT_SECONDS = 30
MAX_DAYS = 366
MAX_ACCOUNT_DISCOVERY_PAGES = 100
MAX_INSIGHT_PAGES = 100
MAX_ACCOUNTS = 500
MAX_BILLING_PROFILES = 5000
MAX_DAILY_ROWS = 50_000
MAX_AD_INSIGHT_ROWS = 50_000
MAX_AD_METADATA_PAGES = 100
MAX_ADS = 50_000
META_SOURCE = "Meta Marketing API Insights"
DECIMAL_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")


@frappe.whitelist(methods=["GET"])
def get_dashboard_setup(company):
    """Return company-scoped setup choices without returning stored secrets."""
    _require_admin()
    company_name = _company_name(company)

    connections = frappe.get_all(
        "LaraTik Meta Ads Connection",
        filters={"company": company_name},
        fields=["name", "graph_api_version", "meta_user_name", "status", "last_validated_at"],
        order_by="modified desc",
        limit_page_length=200,
    )
    accounts = frappe.get_all(
        "LaraTik Meta Ads Account",
        filters={"company": company_name},
        fields=[
            "name", "external_account_id", "account_name", "currency", "timezone",
            "billing_profile", "status", "last_sync_at", "last_sync_status",
        ],
        order_by="account_name asc",
        limit_page_length=MAX_ACCOUNTS,
    )
    profiles = _profile_payloads(company_name)
    profiles_truncated = len(profiles) > MAX_BILLING_PROFILES
    profiles = profiles[:MAX_BILLING_PROFILES]
    customers = frappe.get_all(
        "Customer",
        filters={"disabled": 0},
        fields=["name", "customer_name"],
        order_by="customer_name asc",
        limit_page_length=5000,
    )
    currencies = frappe.get_all(
        "Currency",
        filters={"enabled": 1},
        fields=["name", "currency_name"],
        order_by="name asc",
        limit_page_length=500,
    )
    # The ACCOUNTING POLICY links. They are Link fields, so the reader picks a
    # record out of a list read from ERPNext; a free-text box would only look
    # like a link field, and a name typed by hand is a name nobody validated.
    income_accounts = frappe.get_all(
        "Account",
        filters={"company": company_name, "is_group": 0, "account_type": "Income Account", "disabled": 0},
        fields=["name", "account_name"],
        order_by="account_name asc",
        limit_page_length=500,
    )
    invoice_items = frappe.get_all(
        "Item",
        filters={"disabled": 0, "is_stock_item": 0},
        fields=["name", "item_name"],
        order_by="item_name asc",
        limit_page_length=500,
    )
    tax_templates = frappe.get_all(
        "Sales Taxes and Charges Template",
        filters={"disabled": 0},
        fields=["name", "title"],
        order_by="name asc",
        limit_page_length=200,
    )
    return {
        "company": company_name,
        "connections": connections,
        "accounts": accounts,
        "profiles": profiles,
        "customers": customers,
        "currencies": currencies,
        "income_accounts": income_accounts,
        "invoice_items": invoice_items,
        "tax_templates": tax_templates,
        "truncated": len(accounts) >= MAX_ACCOUNTS or len(customers) >= 5000 or profiles_truncated,
    }


@frappe.whitelist(methods=["POST"])
def save_connection(company, connection_name, access_token, graph_api_version="v26.0"):
    """Validate and store a Meta token in Frappe's encrypted Password field."""
    _require_admin()
    company_name = _company_name(company)
    name = _text(connection_name, "Connection name", 140)
    token = _text(access_token, "Access token", 4096)
    version = _text(graph_api_version, "Graph API version", 20)
    if not name or not token:
        frappe.throw("Enter a connection name and access token.")
    if not re.fullmatch(r"v\d+\.\d+", version):
        frappe.throw("Graph API version must look like v26.0.")

    identity = _meta_get(version, "me", {"fields": "id,name"}, token)
    if not isinstance(identity.get("id"), str) or not identity["id"]:
        frappe.throw("Meta did not validate this access token.")

    if frappe.db.exists("LaraTik Meta Ads Connection", name):
        doc = frappe.get_doc("LaraTik Meta Ads Connection", name)
        doc.check_permission("write")
        if doc.company != company_name:
            frappe.throw("This connection name is already used by a different Company.")
    else:
        doc = frappe.new_doc("LaraTik Meta Ads Connection")
        doc.connection_name = name
        doc.company = company_name

    doc.graph_api_version = version
    doc.access_token = token
    doc.access_token_last4 = token[-4:]
    doc.meta_user_id = identity["id"]
    doc.meta_user_name = str(identity.get("name") or "")[:140]
    doc.status = "Connected"
    doc.last_validated_at = now_datetime()
    doc.save()
    return {"connection": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def discover_ad_accounts(company, connection_name):
    """Discover and upsert every account exposed to a connected Meta identity."""
    _require_admin()
    company_name = _company_name(company)
    connection = _connection(company_name, connection_name)
    token = _token(connection)
    version = connection.graph_api_version
    accounts, complete = _discover_accounts(version, token)
    if not complete:
        frappe.throw("Meta account discovery reached its page limit; no complete list is available.")
    if len(accounts) > MAX_ACCOUNTS:
        frappe.throw("Meta returned more accounts than this setup page can safely process.")

    saved = []
    for raw in accounts:
        external_id = _ad_account_id(raw)
        existing = frappe.get_all(
            "LaraTik Meta Ads Account",
            filters={"external_account_id": external_id},
            fields=["name", "company", "connection"],
            limit_page_length=2,
        )
        if len(existing) > 1:
            frappe.throw("More than one ERPNext account is mapped to a Meta ad account ID.")
        if existing:
            if existing[0].company != company_name:
                frappe.throw("A discovered Meta ad account is already assigned to another Company.")
            doc = frappe.get_doc("LaraTik Meta Ads Account", existing[0].name)
            doc.check_permission("write")
            doc.connection = connection.name
        else:
            doc = frappe.new_doc("LaraTik Meta Ads Account")
            doc.company = company_name
            doc.connection = connection.name
            doc.external_account_id = external_id
            doc.status = "Active"

        doc.account_name = _text(raw.get("name") or external_id, "Meta account name", 140)
        doc.currency = _currency_code(raw.get("currency"))
        doc.timezone = _text(raw.get("timezone_name") or "", "Account time zone", 80)
        doc.save()
        saved.append({"name": doc.name, "external_account_id": external_id, "account_name": doc.account_name})

    return {"connection": connection.name, "accounts": saved, "complete": True}


@frappe.whitelist(methods=["POST"])
def add_ad_account(company, connection_name, external_account_id):
    """Validate and upsert one known account when Meta does not list it."""
    _require_admin()
    company_name = _company_name(company)
    connection = _connection(company_name, connection_name)
    token = _token(connection)
    external_id = _normalize_ad_account_id(external_account_id)
    raw = _meta_get(
        connection.graph_api_version,
        external_id,
        {"fields": "id,name,account_id,currency,timezone_name,account_status"},
        token,
    )
    if _ad_account_id(raw) != external_id:
        frappe.throw("Meta returned a different ad account than the one requested.")

    existing = frappe.get_all(
        "LaraTik Meta Ads Account",
        filters={"external_account_id": external_id},
        fields=["name", "company"],
        limit_page_length=2,
    )
    if len(existing) > 1:
        frappe.throw("More than one ERPNext account is mapped to this Meta ad account ID.")
    if existing:
        if existing[0].company != company_name:
            frappe.throw("This Meta ad account is already assigned to another Company.")
        account = frappe.get_doc("LaraTik Meta Ads Account", existing[0].name)
        account.check_permission("write")
        account.connection = connection.name
    else:
        accounts = frappe.get_all(
            "LaraTik Meta Ads Account",
            filters={"company": company_name},
            fields=["name"],
            limit_page_length=MAX_ACCOUNTS,
        )
        if len(accounts) >= MAX_ACCOUNTS:
            frappe.throw("This Company has reached the supported Meta ad account limit.")
        account = frappe.new_doc("LaraTik Meta Ads Account")
        account.company = company_name
        account.connection = connection.name
        account.external_account_id = external_id

    account.account_name = _text(raw.get("name") or external_id, "Meta account name", 140)
    account.currency = _currency_code(raw.get("currency"))
    account.timezone = _text(raw.get("timezone_name") or "", "Account time zone", 80)
    account_status = raw.get("account_status")
    account.status = "Active" if account_status in (None, 1, "1") else "Paused"
    account.save()
    return {
        "account": account.name,
        "external_account_id": external_id,
        "account_name": account.account_name,
        "currency": account.currency,
        "timezone": account.timezone,
        "status": account.status,
    }


@frappe.whitelist(methods=["POST"])
def save_billing_profile(company, customer, relationship_kind, fee_percent, invoice_currency,
                         page_assets, fee_rules=None, profile_name=None):
    """Create/update an ERPNext Customer billing profile and its page mappings."""
    _require_admin()
    company_name = _company_name(company)
    customer_name = _text(customer, "Billing Customer", 140)
    relationship = _text(relationship_kind, "Relationship", 40)
    currency = _currency_code(invoice_currency)
    if relationship not in {"Customer", "Third Party"}:
        frappe.throw("Relationship must be Customer or Third Party.")

    try:
        fee = Decimal(str(fee_percent or "0"))
    except (InvalidOperation, TypeError, ValueError):
        frappe.throw("Enter a valid service fee percentage.")
    if not fee.is_finite() or fee < 0 or fee > 100:
        frappe.throw("Service fee must be between 0% and 100%.")
    normalized_fee_rules = _parse_fee_rules(fee_rules) if fee_rules is not None else None

    customer_doc = frappe.get_doc("Customer", customer_name)
    customer_doc.check_permission("read")
    if customer_doc.disabled:
        frappe.throw("Choose an enabled billing Customer.")
    if not frappe.db.get_value("Currency", currency, "enabled"):
        frappe.throw("Choose an enabled invoice currency.")

    assets = _parse_page_assets(page_assets)
    if profile_name:
        profile = frappe.get_doc("LaraTik Meta Ads Billing Profile", _text(profile_name, "Billing profile", 140))
        profile.check_permission("write")
        if profile.company != company_name:
            frappe.throw("The billing profile belongs to another Company.")
    else:
        profile = frappe.new_doc("LaraTik Meta Ads Billing Profile")
        profile.company = company_name

    profile.customer = customer_name
    profile.relationship_kind = relationship
    profile.fee_percent = str(fee)
    profile.invoice_currency = currency
    profile.disabled = 0
    profile.set("page_assets", [])
    for asset in assets:
        profile.append("page_assets", asset)
    if normalized_fee_rules is not None or not profile_name:
        profile.set("fee_rules", [])
        for rule in normalized_fee_rules or []:
            profile.append("fee_rules", rule)
    profile.save()
    return {"profile": profile.name, "customer": customer_name}


@frappe.whitelist(methods=["POST"])
def save_billing_fee_rules(company, profile_name, fee_rules, fee_percent=None):
    """Replace a profile's fee schedule without changing its Pages or policy."""
    _require_admin()
    company_name = _company_name(company)
    profile = frappe.get_doc("LaraTik Meta Ads Billing Profile", _text(profile_name, "Billing profile", 140))
    profile.check_permission("write")
    if profile.company != company_name:
        frappe.throw("The billing profile belongs to another Company.")

    normalized_fee_rules = _parse_fee_rules(fee_rules)
    normalized_base_fee = None
    if fee_percent is not None:
        try:
            normalized_base_fee = Decimal(str(fee_percent))
        except (InvalidOperation, TypeError, ValueError):
            frappe.throw("Enter a valid service fee percentage.")
        if not normalized_base_fee.is_finite() or normalized_base_fee < 0 or normalized_base_fee > 100:
            frappe.throw("Service fee must be between 0% and 100%.")

    if normalized_base_fee is not None:
        profile.fee_percent = format(normalized_base_fee, "f")
    profile.set("fee_rules", [])
    for rule in normalized_fee_rules:
        profile.append("fee_rules", rule)
    profile.save()
    return {"profile": profile.name, "fee_rule_count": len(normalized_fee_rules)}


@frappe.whitelist(methods=["POST"])
def save_billing_policy(company, profile_name, charge_model=None, recognition_model=None,
                        income_account=None, invoice_item=None, tax_treatment=None,
                        tax_template=None, fee_is_configured=None):
    """Record the ACCOUNTING POLICY a billing profile runs under.

    ─── ADR-232: A POLICY ANSWER IS A FIELD NOBODY FILLED IN ──────────────────

    An UNSET field means "not decided". It is never defaulted here, and the app
    never picks one: a profile whose policy is unanswered cannot produce a
    draft, and that is the intended behaviour, not a defect.

    So this endpoint knows THREE states, and the difference between them is the
    whole point of it:

    - NOT PROVIDED (`None`) — the caller is not answering this question. The
      stored answer is left exactly as it was, so a caller that answers ONE
      question does not silently clear the other six.
    - PROVIDED AND EMPTY (`""`) — the reader explicitly chose "not decided".
      The field is CLEARED, so the profile goes back to being undecided.
    - PROVIDED WITH A VALUE — the answer is checked against the CLOSED
      vocabulary the invoice workflow reads, and stored.

    The transport makes the first two distinguishable: the web app sends a JSON
    body, so an absent key stays absent and an empty string stays an empty
    string. Nothing here has to guess which one it was handed.

    ─── WHY THE VOCABULARY IS IMPORTED, NOT RE-TYPED ──────────────────────────

    `meta_ads_invoicing` owns the three vocabularies — the charge models, the
    recognition models and the tax treatments — and the invoice workflow reads
    the same constants to decide whether a period is ready. A second, typed-out
    copy of those lists would be a second answer to "what may be chosen here",
    and the two could drift apart without anything noticing.
    """
    _require_admin()
    company_name = _company_name(company)
    profile = frappe.get_doc("LaraTik Meta Ads Billing Profile", _text(profile_name, "Billing profile", 140))
    profile.check_permission("write")
    if profile.company != company_name:
        frappe.throw("The billing profile belongs to another Company.")

    charge = _policy_choice(charge_model, "Charge model", invoicing.CHARGE_MODELS)
    if charge is not None:
        profile.charge_model = charge

    recognition = _policy_choice(recognition_model, "Recognition model", invoicing.RECOGNITION_MODELS)
    if recognition is not None:
        profile.recognition_model = recognition

    treatment = _policy_choice(tax_treatment, "Tax treatment", invoicing.TAX_TREATMENTS)
    if treatment is not None:
        profile.tax_treatment = treatment

    if income_account is not None:
        account_name = _text(income_account, "Income account", 140)
        if account_name:
            account = frappe.db.get_value("Account", account_name, ["company", "disabled"], as_dict=True)
            if not account:
                frappe.throw("Choose an existing income Account.")
            if account.get("disabled"):
                frappe.throw("Choose an enabled income Account.")
            if account.get("company") != company_name:
                frappe.throw("The income Account belongs to another Company.")
        profile.income_account = account_name

    if invoice_item is not None:
        item_name = _text(invoice_item, "Invoice item", 140)
        if item_name:
            item = frappe.db.get_value("Item", item_name, ["disabled"], as_dict=True)
            if not item:
                frappe.throw("Choose an existing invoice Item.")
            if item.get("disabled"):
                frappe.throw("Choose an enabled invoice Item.")
        profile.invoice_item = item_name

    if tax_template is not None:
        template_name = _text(tax_template, "Tax template", 140)
        if template_name:
            template = frappe.db.get_value(
                "Sales Taxes and Charges Template", template_name, ["company", "disabled"], as_dict=True
            )
            if not template:
                frappe.throw("Choose an existing Sales Taxes and Charges Template.")
            if template.get("disabled"):
                frappe.throw("Choose an enabled Sales Taxes and Charges Template.")
            if template.get("company") and template.get("company") != company_name:
                frappe.throw("The tax template belongs to another Company.")
        profile.tax_template = template_name

    if fee_is_configured is not None:
        profile.fee_is_configured = _check_flag(fee_is_configured)

    profile.save()
    return {
        "profile": profile.name,
        "charge_model": profile.charge_model or "",
        "recognition_model": profile.recognition_model or "",
        "income_account": profile.income_account or "",
        "invoice_item": profile.invoice_item or "",
        "tax_treatment": profile.tax_treatment or "",
        "tax_template": profile.tax_template or "",
        "fee_is_configured": int(bool(profile.fee_is_configured)),
    }


@frappe.whitelist(methods=["POST"])
def map_ad_account(company, account_name, profile_name):
    """Map one company-scoped Meta ad account to a configured billing profile."""
    _require_admin()
    company_name = _company_name(company)
    account = frappe.get_doc("LaraTik Meta Ads Account", _text(account_name, "Ad account", 140))
    account.check_permission("write")
    if account.company != company_name:
        frappe.throw("The Meta ad account belongs to another Company.")
    profile = frappe.get_doc("LaraTik Meta Ads Billing Profile", _text(profile_name, "Billing profile", 140))
    profile.check_permission("read")
    if profile.company != company_name or profile.disabled:
        frappe.throw("Choose an enabled billing profile for this Company.")
    account.billing_profile = profile.name
    account.save()
    return {"account": account.name, "billing_profile": profile.name}


@frappe.whitelist(methods=["POST"])
def sync_spend(company, since, until, account_name=None):
    """Fetch daily Insights spend and store one auditable snapshot per account/day."""
    _require_admin()
    company_name = _company_name(company)
    start, end = _date_range(since, until)
    if account_name:
        account = frappe.get_doc("LaraTik Meta Ads Account", _text(account_name, "Ad account", 140))
        account.check_permission("read")
        if account.company != company_name:
            frappe.throw("The Meta ad account belongs to another Company.")
        accounts = [account]
    else:
        account_names = frappe.get_all(
            "LaraTik Meta Ads Account",
            filters={"company": company_name},
            fields=["name"],
            order_by="name asc",
            limit_page_length=MAX_ACCOUNTS + 1,
        )
        if len(account_names) > MAX_ACCOUNTS:
            frappe.throw("There are too many ad accounts to sync at once; select one account.")
        accounts = [frappe.get_doc("LaraTik Meta Ads Account", row.name) for row in account_names]

    if not accounts:
        frappe.throw("Discover at least one Meta ad account before syncing spend.")

    profile_snapshots = _profile_payloads(company_name)
    if len(profile_snapshots) > MAX_BILLING_PROFILES:
        frappe.throw("There are too many billing profiles to safely map Page spend; narrow the setup before syncing.")
    profiles_by_name = {profile["name"]: profile for profile in profile_snapshots}
    page_profile_assets = _page_profile_assets(profile_snapshots)

    results = []
    for index, account in enumerate(accounts):
        savepoint = f"meta_ads_{index}"
        frappe.db.savepoint(savepoint)
        fetched_at = now_datetime()
        log = frappe.new_doc("LaraTik Meta Ads Sync")
        log.company = company_name
        log.ad_account = account.name
        log.external_account_id = account.external_account_id
        log.from_date = start
        log.to_date = end
        log.fetched_at = fetched_at
        log.status = "Pending"
        log.row_count = 0
        log.source = META_SOURCE
        log.insert()
        try:
            connection = _connection(company_name, account.connection)
            token = _token(connection)
            daily = _read_daily_spend(
                connection.graph_api_version, token, account.external_account_id,
                start.isoformat(), end.isoformat(),
            )
            profile_name = account.billing_profile or None
            profile_snapshot = profiles_by_name.get(profile_name) if profile_name else None
            if profile_snapshot and profile_snapshot["disabled"]:
                profile_name = None
                profile_snapshot = None

            for spend_date, amount in sorted(daily.items()):
                row = frappe.new_doc("LaraTik Meta Ads Spend")
                row.snapshot_key = f"{log.name}|{account.external_account_id}|{spend_date}"
                row.sync_run = log.name
                row.company = company_name
                row.ad_account = account.name
                row.external_account_id = account.external_account_id
                row.level = "Account"
                row.attribution_status = "Not Applicable"
                row.billing_profile = profile_name
                row.billing_customer_snapshot = profile_snapshot["customer"] if profile_snapshot else None
                row.relationship_kind_snapshot = profile_snapshot["relationship_kind"] if profile_snapshot else None
                if profile_snapshot:
                    row.fee_status_snapshot, row.fee_percent_snapshot = _fee_status_for_date(
                        profile_snapshot, spend_date
                    )
                else:
                    row.fee_status_snapshot = None
                    row.fee_percent_snapshot = None
                row.invoice_currency_snapshot = profile_snapshot["invoice_currency"] if profile_snapshot else None
                row.page_assets_snapshot = json.dumps(profile_snapshot["page_assets"], ensure_ascii=False) if profile_snapshot else "[]"
                row.spend_date = spend_date
                row.amount = format(amount, "f")
                row.currency = account.currency
                row.source = META_SOURCE
                row.fetched_at = fetched_at
                row.insert()

            ad_status = "Complete"
            ad_message = ""
            ad_rows = []
            reconciliation_status = "Not Checked"
            reconciliation_difference = ""
            reconciliation_mismatched_days = 0
            ad_savepoint = f"{savepoint}_ads"
            frappe.db.savepoint(ad_savepoint)
            try:
                ad_rows = _read_daily_ad_spend(
                    connection.graph_api_version, token, account.external_account_id,
                    start.isoformat(), end.isoformat(),
                )
                if len(ad_rows) > MAX_AD_INSIGHT_ROWS:
                    raise ValueError("Meta returned more ad-level rows than this sync can safely store.")
                reconciliation_status, reconciliation_difference, reconciliation_mismatched_days = _reconcile_ad_spend(daily, ad_rows, account.currency)
                for ad_row in ad_rows:
                    attribution = _attribute_ad_page(ad_row, page_profile_assets)
                    matched_asset = attribution["asset"]
                    ad_profile_snapshot = matched_asset.get("_billing_profile") if matched_asset else None
                    asset_snapshot = {
                        key: matched_asset.get(key, "")
                        for key in ("page_id", "page_name", "instagram_account_id", "instagram_username", "served_customer")
                    } if matched_asset else None
                    asset_rows = ad_profile_snapshot["page_assets"] if ad_profile_snapshot else []
                    row = frappe.new_doc("LaraTik Meta Ads Spend")
                    row.snapshot_key = f"{log.name}|{account.external_account_id}|ad|{ad_row['ad_id']}|{ad_row['spend_date']}"
                    row.sync_run = log.name
                    row.company = company_name
                    row.ad_account = account.name
                    row.external_account_id = account.external_account_id
                    row.level = "Ad"
                    row.campaign_id = ad_row["campaign_id"]
                    row.campaign_name = ad_row["campaign_name"]
                    row.adset_id = ad_row["adset_id"]
                    row.adset_name = ad_row["adset_name"]
                    row.ad_id = ad_row["ad_id"]
                    row.ad_name = ad_row["ad_name"]
                    row.creative_id = ad_row["creative_id"]
                    row.creative_story_id = ad_row["creative_story_id"]
                    row.page_id = attribution["page_id"]
                    row.page_name = attribution["page_name"]
                    row.instagram_actor_id = attribution["instagram_actor_id"]
                    row.attribution_status = attribution["status"]
                    row.page_asset_snapshot = json.dumps(asset_snapshot, ensure_ascii=False) if asset_snapshot else ""
                    row.billing_profile = ad_profile_snapshot["name"] if ad_profile_snapshot else None
                    row.billing_customer_snapshot = ad_profile_snapshot["customer"] if ad_profile_snapshot else None
                    row.relationship_kind_snapshot = ad_profile_snapshot["relationship_kind"] if ad_profile_snapshot else None
                    if ad_profile_snapshot:
                        row.fee_status_snapshot, row.fee_percent_snapshot = _fee_status_for_date(
                            ad_profile_snapshot, ad_row["spend_date"]
                        )
                    else:
                        row.fee_status_snapshot = None
                        row.fee_percent_snapshot = None
                    row.invoice_currency_snapshot = ad_profile_snapshot["invoice_currency"] if ad_profile_snapshot else None
                    row.page_assets_snapshot = json.dumps(asset_rows, ensure_ascii=False)
                    row.spend_date = ad_row["spend_date"]
                    row.amount = format(ad_row["amount"], "f")
                    row.currency = account.currency
                    row.source = META_SOURCE
                    row.fetched_at = fetched_at
                    row.insert()
            except Exception as error:
                # Account spend remains usable if the separate ad-level read
                # is refused or incomplete. The page report will fail closed.
                frappe.db.rollback(save_point=ad_savepoint)
                ad_status = "Failed"
                ad_rows = []
                reconciliation_status = "Not Checked"
                reconciliation_difference = ""
                reconciliation_mismatched_days = 0
                ad_message = _meta_error_status(error)

            log.status = "Complete"
            log.row_count = len(daily)
            log.message = f"Read complete: {len(daily)} daily Meta Insights rows."
            log.ad_insights_status = ad_status
            log.ad_row_count = len(ad_rows)
            log.ad_reconciliation_status = reconciliation_status
            log.ad_reconciliation_difference = reconciliation_difference
            log.ad_reconciliation_mismatched_days = reconciliation_mismatched_days
            log.ad_message = ad_message or f"Read complete: {len(ad_rows)} daily ad-level rows."
            log.save()
            account.last_sync_at = fetched_at
            account.last_sync_status = "Complete"
            account.last_sync_message = log.message
            account.save()
            results.append({
                "account": account.name,
                "status": "Complete",
                "row_count": len(daily),
                "ad_insights_status": ad_status,
                "ad_row_count": len(ad_rows),
                "ad_reconciliation_status": reconciliation_status,
            })
        except Exception as error:
            frappe.db.rollback(save_point=savepoint)
            # No exception text or request URL is stored: HTTP libraries can
            # include query data in exceptions, and a Meta token is a secret.
            status = _meta_error_status(error)
            log = frappe.new_doc("LaraTik Meta Ads Sync")
            log.company = company_name
            log.ad_account = account.name
            log.external_account_id = account.external_account_id
            log.from_date = start
            log.to_date = end
            log.fetched_at = fetched_at
            log.status = "Failed"
            log.row_count = 0
            log.message = status
            log.ad_insights_status = "Failed"
            log.ad_row_count = 0
            log.ad_reconciliation_status = "Not Checked"
            log.ad_reconciliation_mismatched_days = 0
            log.ad_message = status
            log.source = META_SOURCE
            log.insert()
            account.last_sync_at = fetched_at
            account.last_sync_status = "Failed"
            account.last_sync_message = status
            account.save()
            results.append({"account": account.name, "status": "Failed", "row_count": 0, "ad_insights_status": "Failed"})

    return {"company": company_name, "since": start.isoformat(), "until": end.isoformat(), "results": results}


@frappe.whitelist(methods=["GET"])
def get_meta_ads_context(company):
    """The workspace scope: company, reporting currency, accounts, last sync.

    ─── WHY THIS IS ONE CHEAP READ AND NOT THE SPEND REPORT ─────────────────────

    Every section of the Meta Ads workspace has to state the SAME scope: the
    company, the currency the report is read in, the accounts it covers, and
    when the data was last synchronised. Before this method, each section
    built that scope from whichever report it happened to read — so the
    account selector vanished on the settlement screen, because that screen
    read the billing report and the billing report does not carry the
    account's sync state.

    A control that disappears when the reader changes tabs reads as a
    statement about the DATA. It is a statement about the LAYOUT, and this
    read removes it: one small query over the account records, no Insights
    rows, no ad-level spend, no reconciliation — and every section states the
    same scope from it.
    """
    _require_admin()
    company_name = _company_name(company)
    rows = frappe.get_all(
        "LaraTik Meta Ads Account",
        filters={"company": company_name},
        fields=[
            "name", "external_account_id", "account_name", "currency", "timezone",
            "status", "last_sync_at", "last_sync_status",
        ],
        order_by="account_name asc",
        limit_page_length=MAX_ACCOUNTS + 1,
    )
    truncated = len(rows) > MAX_ACCOUNTS
    rows = rows[:MAX_ACCOUNTS]

    latest_sync_at = ""
    latest_sync_status = ""
    for row in rows:
        if (row.last_sync_status or "") != "Complete":
            continue
        fetched = str(row.last_sync_at or "")
        if fetched > latest_sync_at:
            latest_sync_at = fetched
            latest_sync_status = row.last_sync_status or ""

    return {
        "company": company_name,
        "reporting_currency": frappe.db.get_value("Company", company_name, "default_currency") or "",
        "accounts": [
            {
                "name": row.name,
                "external_account_id": row.external_account_id or "",
                "account_name": row.account_name or "",
                "currency": row.currency or "",
                "timezone": row.timezone or "",
                "status": row.status or "",
                "last_sync_at": str(row.last_sync_at or ""),
                "last_sync_status": row.last_sync_status or "",
            }
            for row in rows
        ],
        "accounts_truncated": truncated,
        "last_sync_at": latest_sync_at,
        "last_sync_status": latest_sync_status,
    }


def _period_accounts(company_name, start, end, account_name=None):
    """The company's ad accounts, and the sync runs that completed for them.

    ─── WHY THIS IS A NAMED FUNCTION ───────────────────────────────────────────

    The customer invoice workspace reads the same period the spend report
    reads, and it has to reach the same accounts and the same completed runs.
    Two copies of this query would be two answers to "which spend does this
    period hold", and a report that says one thing while an invoice says
    another is a reconciliation nobody can close.

    So the selection lives here, once, and both callers use it.
    """
    account_filters = {"company": company_name}
    account_fields = [
        "name", "external_account_id", "account_name", "currency", "timezone",
        "billing_profile", "status", "last_sync_at", "last_sync_status",
    ]
    if account_name:
        selected = frappe.get_doc("LaraTik Meta Ads Account", _text(account_name, "Ad account", 140))
        selected.check_permission("read")
        if selected.company != company_name:
            frappe.throw("The Meta ad account belongs to another Company.")
        account_filters["name"] = selected.name

    # The selector needs every company account even when the report query is
    # filtered to one selected account. Keep these two scopes independent.
    account_option_rows = frappe.get_all(
        "LaraTik Meta Ads Account",
        filters={"company": company_name},
        fields=account_fields,
        order_by="account_name asc",
        limit_page_length=MAX_ACCOUNTS + 1,
    )
    account_options_truncated = len(account_option_rows) > MAX_ACCOUNTS
    if account_name:
        account_rows = frappe.get_all(
            "LaraTik Meta Ads Account",
            filters=account_filters,
            fields=account_fields,
            order_by="account_name asc",
            limit_page_length=MAX_ACCOUNTS + 1,
        )
    else:
        account_rows = account_option_rows
    accounts_truncated = len(account_rows) > MAX_ACCOUNTS
    account_rows = account_rows[:MAX_ACCOUNTS]
    account_option_rows = account_option_rows[:MAX_ACCOUNTS]

    complete_runs = frappe.get_all(
        "LaraTik Meta Ads Sync",
        filters={
            "company": company_name,
            "status": "Complete",
            "from_date": ["<=", start.isoformat()],
            "to_date": [">=", end.isoformat()],
        },
        fields=[
            "name", "external_account_id", "fetched_at", "row_count", "ad_insights_status",
            "ad_row_count", "ad_reconciliation_status",
        ],
        order_by="fetched_at desc",
        limit_page_length=20_001,
    )
    runs_truncated = len(complete_runs) > 20_000
    complete_runs = complete_runs[:20_000]
    complete_by_account = {}
    for run in complete_runs:
        complete_by_account.setdefault(run.external_account_id, run)

    return {
        "accounts": account_rows,
        "selected_account_name": selected.name if account_name else "",
        "account_options": account_option_rows,
        "account_options_truncated": account_options_truncated,
        "accounts_truncated": accounts_truncated,
        "complete_by_account": complete_by_account,
        "runs_truncated": runs_truncated,
        "selected_run_names": [run.name for run in complete_by_account.values()],
    }


@frappe.whitelist(methods=["GET"])
def get_spend_report(company, since, until, account_name=None):
    """Read a company-scoped daily ledger and compute exact, dated billing previews."""
    _require_admin()
    company_name = _company_name(company)
    start, end = _date_range(since, until)
    period = _period_accounts(company_name, start, end, account_name)
    account_rows = period["accounts"]
    account_option_rows = period["account_options"]
    account_options_truncated = period["account_options_truncated"]
    accounts_truncated = period["accounts_truncated"]
    complete_by_account = period["complete_by_account"]
    runs_truncated = period["runs_truncated"]
    selected_run_names = period["selected_run_names"]

    spend_rows = []
    if selected_run_names:
        spend_rows = frappe.get_all(
            "LaraTik Meta Ads Spend",
            filters={
                "company": company_name,
                "level": "Account",
                "spend_date": ["between", [start.isoformat(), end.isoformat()]],
                "sync_run": ["in", selected_run_names],
                **({"ad_account": period["selected_account_name"]} if account_name else {}),
            },
            fields=[
                "name", "sync_run", "ad_account", "external_account_id", "billing_profile", "spend_date",
                "billing_customer_snapshot", "relationship_kind_snapshot", "fee_percent_snapshot",
                "invoice_currency_snapshot", "page_assets_snapshot", "amount", "currency", "source", "fetched_at",
            ],
            order_by="spend_date asc, ad_account asc",
            limit_page_length=MAX_DAILY_ROWS + 1,
        )
    rows_truncated = len(spend_rows) > MAX_DAILY_ROWS
    spend_rows = spend_rows[:MAX_DAILY_ROWS]

    ad_spend_rows = _period_ad_spend_rows(
        company_name, start, end, selected_run_names, period["selected_account_name"]
    )
    ad_rows_truncated = len(ad_spend_rows) > MAX_AD_INSIGHT_ROWS
    ad_spend_rows = ad_spend_rows[:MAX_AD_INSIGHT_ROWS]

    account_by_name = {row.name: row for row in account_rows}
    account_status = {}
    report_accounts = []
    for account in account_rows:
        run = complete_by_account.get(account.external_account_id)
        account_status[account.name] = "Complete" if run else "Not synced for this period"
        report_accounts.append({
            "name": account.name,
            "external_account_id": account.external_account_id,
            "account_name": account.account_name,
            "currency": account.currency,
            "timezone": account.timezone or "",
            "billing_profile": account.billing_profile or "",
            "status": account.status,
            "last_sync_at": str(account.last_sync_at or ""),
            "last_sync_status": account.last_sync_status or "",
            "period_sync_status": account_status[account.name],
            "period_synced_at": str(run.fetched_at or "") if run else "",
        })

    account_options = [{
        "name": account.name,
        "external_account_id": account.external_account_id,
        "account_name": account.account_name,
        "currency": account.currency,
        "timezone": account.timezone or "",
        "billing_profile": account.billing_profile or "",
        "status": account.status,
        "last_sync_at": str(account.last_sync_at or ""),
        "last_sync_status": account.last_sync_status or "",
    } for account in account_option_rows]

    exact_rows = []
    spend_by_currency = {}
    unreadable_count = 0
    for raw in spend_rows:
        account = account_by_name.get(raw.ad_account)
        if not account:
            continue
        try:
            amount = _decimal(raw.amount)
            currency = _stored_currency_code(raw.currency)
            if amount < 0 or not currency or currency != account.currency:
                raise ValueError("Daily spend currency or sign is invalid.")
        except (ValueError, InvalidOperation, TypeError):
            unreadable_count += 1
            continue
        amount_text = format(amount, "f")
        spend_by_currency[currency] = spend_by_currency.get(currency, Decimal(0)) + amount
        exact_rows.append({
            "account": raw.ad_account,
            "external_account_id": raw.external_account_id,
            "account_name": account.account_name,
            "profile": "",
            "billing_group": "",
            "spend_date": str(raw.spend_date),
            "amount": amount_text,
            "currency": currency,
            "source": raw.source or META_SOURCE,
            "fetched_at": str(raw.fetched_at or ""),
        })

    run_by_account = {
        account.name: complete_by_account.get(account.external_account_id)
        for account in account_rows
    }
    billable_rows, billing_unreadable_count = _billable_ad_rows(
        ad_spend_rows, account_by_name, run_by_account
    )
    raw_billable_groups, billing_unreadable_count = _collect_ad_billable_groups(
        billable_rows, billing_unreadable_count
    )
    # THE SWITCHED-OFF BUCKET.
    #
    # A disabled payer arrangement is a DECISION: those Pages are not charged.
    # Before, their spend fell into the unassigned bucket — the same number as
    # spend whose Page nobody has ever mapped. One is a decision and the other
    # is a gap, and the brief is explicit that they must not be treated as
    # financially equivalent.
    #
    # So the disabled arrangements' Pages are re-matched against the stored
    # UNMAPPED rows, and their spend is held apart. The stored data is not
    # touched; only the reading of it changes.
    non_billable_assets = _non_billable_page_assets(company_name) if ad_spend_rows else []
    not_billable_totals, _not_billable_pages = _collect_non_billable_spend(
        ad_spend_rows, account_by_name, run_by_account, non_billable_assets
    )
    all_accounts_synced = bool(account_rows) and all(
        account_status[account.name] == "Complete" for account in account_rows
    )
    billing_ready = (
        all_accounts_synced and not accounts_truncated and not runs_truncated and not rows_truncated and
        not ad_rows_truncated and unreadable_count == 0 and billing_unreadable_count == 0 and
        all(
            run_by_account[account.name] and
            run_by_account[account.name].ad_insights_status == "Complete" and
            run_by_account[account.name].ad_reconciliation_status == "Matched"
            for account in account_rows
        )
    )

    billable_totals = {}
    for bucket in raw_billable_groups.values():
        for currency, amount in bucket["source_spend"].items():
            billable_totals[currency] = billable_totals.get(currency, Decimal(0)) + amount
    if any(amount > spend_by_currency.get(currency, Decimal(0)) for currency, amount in billable_totals.items()):
        billing_ready = False
    # The billable and the switched-off buckets are held against the SAME
    # account-level spend, so the two together must never exceed it. If they
    # do, the split is not trustworthy and the whole report withholds it.
    for currency, amount in billable_totals.items():
        held = amount + not_billable_totals.get(currency, Decimal(0))
        if held > spend_by_currency.get(currency, Decimal(0)):
            billing_ready = False

    # THE POLICY, per profile. Which of the two charge models this profile
    # runs under is a COMMERCIAL and ACCOUNTING decision somebody has to
    # make, and the app must not make it: the brief says the principal/agent
    # and fee-only/recharge questions "must remain configurable and subject
    # to approved accounting policy".
    #
    # So the policy is READ, never guessed. An undecided profile reports no
    # invoice amount at all — and says why — rather than quietly showing the
    # recharge figure as though it had been settled.
    profile_policies = _profile_policies(
        company_name, {bucket["name"] for bucket in raw_billable_groups.values()}
    )

    billing_groups = []
    unassigned_by_currency = dict(spend_by_currency)
    if billing_ready:
        for group_key, bucket in raw_billable_groups.items():
            amounts = bucket["source_spend"]
            fee_percent = _decimal(bucket["fee_percent"])
            invoice_currency = bucket["invoice_currency"]
            fees_by_currency = {}
            fx = []
            charge_total = Decimal(0)
            conversion_complete = bool(invoice_currency)
            # WHY a group cannot be invoiced, if it cannot. The state is named
            # by the ONE rule, so the reason and the state cannot disagree.
            blocked_reason = ""
            if not invoice_currency:
                blocked_reason = chargeability.CHARGEABILITY_REASON_BILLING_CURRENCY_MISSING
            charge_model = profile_policies.get(bucket["name"], {}).get("charge_model") or ""
            # The CHARGE MODEL decides what the invoice carries, and an
            # undecided model means the amount is not computed at all.
            charge_model_decided = charge_model in invoicing.CHARGE_MODELS
            for source_currency, amount in sorted(amounts.items()):
                source_precision = _currency_precision(source_currency)
                fee = _quantize(amount * fee_percent / Decimal(100), source_precision) if source_precision is not None else None
                fees_by_currency[source_currency] = format(fee, "f") if fee is not None else None
                rate, rate_date = _selling_rate(source_currency, invoice_currency, end)
                if rate is None or source_precision is None:
                    conversion_complete = False
                    if not blocked_reason:
                        blocked_reason = (
                            chargeability.CHARGEABILITY_REASON_EXCHANGE_RATE_MISSING
                            if rate is None
                            else chargeability.CHARGEABILITY_REASON_CURRENCY_PRECISION_UNKNOWN
                        )
                    fx.append({"from": source_currency, "to": invoice_currency, "rate": None, "date": None})
                    continue
                fx.append({"from": source_currency, "to": invoice_currency, "rate": format(rate, "f"), "date": rate_date})
                if not charge_model_decided:
                    continue
                # The charge, in the ACCOUNT's currency, per the model:
                #   Recharge          → the advertising cost AND the fee;
                #   Service fee only  → the fee ALONE, because the advertising
                #                     cost is the customer's own money.
                if charge_model == invoicing.CHARGE_MODEL_RECHARGE:
                    charge_in_source = amount + (fee if fee is not None else Decimal(0))
                else:
                    charge_in_source = fee if fee is not None else Decimal(0)
                charge_total += charge_in_source * rate
            invoice_precision = _currency_precision(invoice_currency) if invoice_currency else None
            if invoice_precision is None:
                conversion_complete = False
                if not blocked_reason:
                    blocked_reason = chargeability.CHARGEABILITY_REASON_CURRENCY_PRECISION_UNKNOWN
            invoice_amount = _quantize(charge_total, invoice_precision) if conversion_complete and charge_model_decided else None
            # WHY there is no invoice amount, when there is none. The screen
            # shows this next to the empty cell, so a reader is never left
            # guessing whether the period is empty or undecided.
            invoice_amount_reason = ""
            if not charge_model_decided:
                invoice_amount_reason = invoicing.NEEDS_APPROVAL_REASON_CHARGE_MODEL_NOT_DECIDED
            elif not conversion_complete:
                invoice_amount_reason = blocked_reason
            state, reason = chargeability.classify_chargeability({
                "attribution_status": chargeability.ATTRIBUTION_STATUS_ATTRIBUTED,
                "blocked_reason": blocked_reason,
            })
            billing_groups.append({
                "key": group_key,
                **{key: value for key, value in bucket.items() if key != "source_spend"},
                "source_spend": {currency: format(amount, "f") for currency, amount in sorted(amounts.items())},
                "service_fee": fees_by_currency,
                "charge_model": charge_model,
                "invoice_amount": format(invoice_amount, "f") if invoice_amount is not None else None,
                "invoice_amount_reason": invoice_amount_reason,
                "fx": fx,
                "chargeability": state,
                "chargeability_reason": reason,
            })
    if billing_ready:
        for currency, amount in billable_totals.items():
            unassigned_by_currency[currency] = unassigned_by_currency.get(currency, Decimal(0)) - amount
        # The switched-off spend leaves the UNASSIGNED bucket. It is not
        # unassigned: it is assigned, and deliberately not charged.
        for currency, amount in not_billable_totals.items():
            unassigned_by_currency[currency] = unassigned_by_currency.get(currency, Decimal(0)) - amount
    else:
        billing_groups = []
        not_billable_totals = {}

    complete = all_accounts_synced and not accounts_truncated and not runs_truncated and not rows_truncated and unreadable_count == 0
    return {
        "company": company_name,
        "since": start.isoformat(),
        "until": end.isoformat(),
        "complete": complete,
        "billing_ready": billing_ready,
        "unreadable_count": unreadable_count,
        "truncated": accounts_truncated or runs_truncated or rows_truncated or ad_rows_truncated,
        "account_options": account_options,
        "account_options_truncated": account_options_truncated,
        "accounts": report_accounts,
        "rows": exact_rows,
        "spend_by_currency": {currency: format(amount, "f") for currency, amount in sorted(spend_by_currency.items())},
        "billing_groups": sorted(billing_groups, key=lambda row: row["customer_name"].casefold()),
        "unassigned_spend": {currency: format(amount, "f") for currency, amount in sorted(unassigned_by_currency.items()) if amount > 0},
        "not_billable_spend": {currency: format(amount, "f") for currency, amount in sorted(not_billable_totals.items()) if amount > 0},
        "source_label": META_SOURCE,
        "charge_evidence_label": "Insights spend; not a Meta payment receipt",
    }


def _period_ad_spend_rows(company_name, start, end, selected_run_names, account_name=None):
    """The ad-level spend rows of a period, from the runs that completed.

    ─── WHY THIS IS A NAMED FUNCTION ───────────────────────────────────────────

    The customer invoice workspace needs the SAME ad rows the spend report
    reads, for the same period, and it has to be certain they are the same
    ones. Two copies of this query would be two answers to "which spend does
    this period hold", and a report that says €1,000 while an invoice says
    €1,100 is a reconciliation nobody can close.

    So the query lives here, once, and both callers use it.
    """
    if not selected_run_names:
        return []
    return frappe.get_all(
        "LaraTik Meta Ads Spend",
        filters={
            "company": company_name,
            "level": "Ad",
            "spend_date": ["between", [start.isoformat(), end.isoformat()]],
            "sync_run": ["in", selected_run_names],
            **({"ad_account": account_name} if account_name else {}),
        },
        fields=[
            "level", "sync_run", "ad_account", "external_account_id", "billing_profile", "campaign_id",
            "billing_customer_snapshot", "relationship_kind_snapshot", "fee_percent_snapshot",
            "fee_status_snapshot", "invoice_currency_snapshot", "page_asset_snapshot",
            "spend_date", "ad_id", "amount",
            "currency", "attribution_status", "page_id", "page_name", "instagram_actor_id",
        ],
        order_by="spend_date asc, ad_account asc, ad_id asc",
        limit_page_length=MAX_AD_INSIGHT_ROWS + 1,
    )


def _billable_ad_rows(ad_rows, account_by_name, run_by_account):
    """The ad rows that are BILLABLE, normalised, with the unreadable count.

    ─── WHY ONE IMPLEMENTATION ─────────────────────────────────────────────────

    "Which spend may be charged" is a money question, so it gets ONE answer.
    The spend report groups these rows for the payer table; the invoice
    workspace groups them for the invoice lines. If the two disagreed about a
    single row, the invoice would not be the invoice of the report.

    A row is billable when all of these hold:

      - it belongs to a run that COMPLETED for that account;
      - its currency is the account's own currency, and its amount is not
        negative;
      - it is an AD row with an ad id;
      - its attribution status is ATTRIBUTED — an unmapped or ambiguous Page
        is never silently allocated (ADR-228);
      - its creative matched a SAVED Page, and that Page's identity does not
        conflict with the ad row's own identity;
      - its payer snapshot is complete.
    """
    rows = []
    unreadable = 0
    for raw in ad_rows:
        account = account_by_name.get(raw.ad_account)
        run = run_by_account.get(raw.ad_account)
        if (
            not account or not run or raw.level != "Ad" or raw.sync_run != run.name or
            raw.external_account_id != account.external_account_id
        ):
            unreadable += 1
            continue
        try:
            amount = _decimal(raw.amount)
            currency = _stored_currency_code(raw.currency)
            if amount < 0 or not currency or currency != account.currency or not raw.ad_id:
                raise ValueError("Stored ad spend is invalid.")
        except (InvalidOperation, TypeError, ValueError):
            unreadable += 1
            continue
        if raw.attribution_status != "Attributed":
            continue

        asset = _read_attributed_page(raw)
        if not asset:
            unreadable += 1
            continue
        try:
            page_asset = {
                "page_id": _text(asset.get("page_id"), "Facebook Page ID", 100),
                "page_name": _text(asset.get("page_name"), "Facebook Page name", 140),
                "instagram_account_id": _text(asset.get("instagram_account_id"), "Instagram account ID", 100),
                "instagram_username": _text(asset.get("instagram_username"), "Instagram username", 140),
                "served_customer": _text(asset.get("served_customer"), "Customer served", 140),
            }
            if not page_asset["page_id"]:
                raise ValueError("A matched Page snapshot has no Page ID.")
            if raw.page_id and raw.page_id != page_asset["page_id"]:
                raise ValueError("The saved Page identity conflicts with the ad row.")
            if raw.instagram_actor_id and page_asset["instagram_account_id"] and raw.instagram_actor_id != page_asset["instagram_account_id"]:
                raise ValueError("The saved Instagram identity conflicts with the ad row.")
            profile_name = _text(raw.billing_profile or "", "Billing profile", 140)
            customer = _text(raw.billing_customer_snapshot or "", "Billing Customer", 140)
            fee_percent = _decimal(raw.fee_percent_snapshot or "")
            invoice_currency = _stored_currency_code(raw.invoice_currency_snapshot)
            # The fee STATUS travels with the percent. A snapshot written
            # before the status existed carries no status, and an absent
            # status is read conservatively: a fee that is not zero was
            # stated, and a fee that IS zero was nobody's decision.
            fee_status = (raw.fee_status_snapshot or "").strip()
            if not fee_status:
                fee_status = (
                    invoicing.FEE_STATUS_CONFIGURED
                    if fee_percent != 0
                    else invoicing.FEE_STATUS_UNCONFIGURED
                )
            if fee_status not in invoicing.FEE_STATUSES:
                raise ValueError("The stored fee status is not one this application knows.")
            if not profile_name or not customer or fee_percent < 0 or fee_percent > 100:
                raise ValueError("The Page's payer snapshot is incomplete.")
        except (InvalidOperation, TypeError, ValueError):
            unreadable += 1
            continue

        rows.append({
            "account": account.name,
            "external_account_id": account.external_account_id,
            "account_name": account.account_name,
            "currency": currency,
            "amount": amount,
            "profile": profile_name,
            "customer": customer,
            "relationship_kind": raw.relationship_kind_snapshot or "Customer",
            "fee_percent": fee_percent,
            "fee_status": fee_status,
            "invoice_currency": invoice_currency,
            "page_asset": page_asset,
            "sync_run": raw.sync_run,
            "ad_id": raw.ad_id,
        })
    return rows, unreadable


def _collect_ad_billable_groups(billable_rows, unreadable):
    """Group the billable ad rows by payer arrangement.

    The rows arrive already filtered and normalised by `_billable_ad_rows`, so
    this function only decides the GROUPS. It never decides what is billable:
    that answer belongs to one place, and the invoice workspace reads it too.
    """
    groups = {}
    for row in billable_rows:
        profile_name = row["profile"]
        customer = row["customer"]
        fee_percent = row["fee_percent"]
        invoice_currency = row["invoice_currency"]
        group_key = "|".join((profile_name, customer, format(fee_percent, "f"), invoice_currency))
        group = groups.setdefault(group_key, {
            "name": profile_name,
            "customer": customer,
            "customer_name": frappe.db.get_value("Customer", customer, "customer_name") or customer,
            "relationship_kind": row["relationship_kind"],
            "fee_percent": format(fee_percent, "f"),
            "fee_status": invoicing.FEE_STATUS_CONFIGURED,
            "invoice_currency": invoice_currency,
            "page_assets": [],
            "accounts": {},
            "source_spend": defaultdict(Decimal),
        })
        group["source_spend"][row["currency"]] += row["amount"]
        if row["fee_status"] == invoicing.FEE_STATUS_UNCONFIGURED:
            group["fee_status"] = invoicing.FEE_STATUS_UNCONFIGURED
        group["accounts"][row["account"]] = row["account_name"]
        if row["page_asset"] not in group["page_assets"]:
            group["page_assets"].append(row["page_asset"])
    return groups, unreadable


def _attribution_reconciliation(buckets, ad_totals, unreadable_count, truncated, complete):
    """TRACK B — the attribution reconciliation.

    ─── WHAT THIS COMPARES ──────────────────────────────────────────────────────

    Total ad-level delivery spend, against the six states that spend can be in.

    ─── WHY THE SIX STATES, AND NOT THREE ───────────────────────────────────────

    The six states are a PARTITION of the ad spend, and the partition is the
    claim: every euro of ad-level delivery spend lands in exactly one of
    them, and the difference between the two sides is the money that did not.

    A three-way partition — assigned, unassigned, ambiguous — would put a
    switched-off arrangement and a blocked charge in the same "assigned"
    figure, and the report would go back to treating a decision and a defect
    as the same money. The partition has to be the one the screens show.

    ─── WHY IT IS A SELF-CHECK AND NOT A SUM ─────────────────────────────────────

    Every ad row that can be read lands in a page group and in the ad total,
    so the difference is structurally zero today. That is the POINT: it is the
    check that stays zero. The failure it catches is a row that is read and
    then goes NOWHERE — dropped, or counted twice — and no table shows that,
    because a table only shows what survived.

    ─── WHY "INCOMPLETE" IS A SEPARATE ANSWER ───────────────────────────────────

    A row that could not be read has an UNKNOWN amount, so it moves neither
    side of the comparison and the difference stays zero. The reconciliation
    would then say "reconciled" about a period whose spend it cannot fully
    state.

    So an unreadable row makes the answer INCOMPLETE, not reconciled: the
    arithmetic is clean and the coverage is not. Reporting "reconciled" here
    would be a figure the reader could trust too far.
    """
    currencies = sorted(set(ad_totals) | {currency for bucket in buckets.values() for currency in bucket})
    difference = {}
    for currency in currencies:
        placed = sum((buckets[state].get(currency, Decimal(0)) for state in chargeability.CHARGEABILITY_STATES), Decimal(0))
        difference[currency] = ad_totals.get(currency, Decimal(0)) - placed

    has_difference = any(value != 0 for value in difference.values())
    if truncated or not complete:
        status = "Unavailable"
    elif unreadable_count:
        status = "Incomplete"
    elif has_difference:
        status = "Difference"
    else:
        status = "Reconciled"

    return {
        "status": status,
        "ad_spend": {currency: format(amount, "f") for currency, amount in sorted(ad_totals.items())},
        "difference": {
            currency: format(amount, "f")
            for currency, amount in sorted(difference.items())
            if amount != 0
        },
        "unreadable_rows": unreadable_count,
        "truncated": truncated,
        "buckets": {
            state: {currency: format(amount, "f") for currency, amount in sorted(buckets[state].items())}
            for state in chargeability.CHARGEABILITY_STATES
        },
    }


@frappe.whitelist(methods=["GET"])
def get_page_spend_report(company, since, until, account_name=None):
    """Group ad-level delivery cost by the Page identity used by each ad.

    Account-level daily Insights from the same fetch run are kept beside the
    ad rows so this report can show whether the per-ad amounts reconcile.
    """
    _require_admin()
    company_name = _company_name(company)
    start, end = _date_range(since, until)
    account_filters = {"company": company_name}
    if account_name:
        selected = frappe.get_doc("LaraTik Meta Ads Account", _text(account_name, "Ad account", 140))
        selected.check_permission("read")
        if selected.company != company_name:
            frappe.throw("The Meta ad account belongs to another Company.")
        account_filters["name"] = selected.name

    account_rows = frappe.get_all(
        "LaraTik Meta Ads Account",
        filters=account_filters,
        fields=["name", "external_account_id", "account_name", "currency", "timezone"],
        order_by="account_name asc",
        limit_page_length=MAX_ACCOUNTS + 1,
    )
    accounts_truncated = len(account_rows) > MAX_ACCOUNTS
    account_rows = account_rows[:MAX_ACCOUNTS]
    account_by_name = {row.name: row for row in account_rows}

    runs = frappe.get_all(
        "LaraTik Meta Ads Sync",
        filters={
            "company": company_name,
            "status": "Complete",
            "from_date": ["<=", start.isoformat()],
            "to_date": [">=", end.isoformat()],
        },
        fields=[
            "name", "ad_account", "external_account_id", "fetched_at", "ad_insights_status",
            "ad_row_count", "ad_reconciliation_status", "ad_reconciliation_difference", "ad_message",
        ],
        order_by="fetched_at desc",
        limit_page_length=20_001,
    )
    runs_truncated = len(runs) > 20_000
    runs = runs[:20_000]
    latest_by_account = {}
    for run in runs:
        latest_by_account.setdefault(run.ad_account, run)
    selected_runs = [latest_by_account[account.name] for account in account_rows if account.name in latest_by_account]
    run_names = [run.name for run in selected_runs]

    rows_truncated = False
    ad_rows = []
    account_spend_rows = []
    if run_names:
        query_filters = {
            "company": company_name,
            "spend_date": ["between", [start.isoformat(), end.isoformat()]],
            "sync_run": ["in", run_names],
        }
        ad_rows = frappe.get_all(
            "LaraTik Meta Ads Spend",
            filters={**query_filters, "level": "Ad"},
            fields=[
                "name", "sync_run", "ad_account", "external_account_id", "billing_profile",
                "billing_customer_snapshot", "campaign_id", "campaign_name", "adset_id", "adset_name",
                "ad_id", "ad_name", "creative_id", "creative_story_id", "page_id", "page_name", "instagram_actor_id",
                "attribution_status", "page_asset_snapshot", "spend_date", "amount", "currency",
            ],
            order_by="spend_date asc, ad_account asc, campaign_name asc, ad_name asc",
            limit_page_length=MAX_AD_INSIGHT_ROWS + 1,
        )
        account_spend_rows = frappe.get_all(
            "LaraTik Meta Ads Spend",
            filters={**query_filters, "level": "Account"},
            fields=["sync_run", "ad_account", "spend_date", "amount", "currency"],
            order_by="spend_date asc, ad_account asc",
            limit_page_length=MAX_DAILY_ROWS + 1,
        )
    rows_truncated = len(ad_rows) > MAX_AD_INSIGHT_ROWS or len(account_spend_rows) > MAX_DAILY_ROWS
    ad_rows = ad_rows[:MAX_AD_INSIGHT_ROWS]
    account_spend_rows = account_spend_rows[:MAX_DAILY_ROWS]

    runs_by_account = {run.ad_account: run for run in selected_runs}
    account_totals = {}
    account_daily = defaultdict(lambda: defaultdict(Decimal))
    page_groups = {}
    campaign_groups = {}
    ad_ids_by_group = defaultdict(set)
    unreadable_count = 0
    for raw in account_spend_rows:
        account = account_by_name.get(raw.ad_account)
        if not account:
            unreadable_count += 1
            continue
        try:
            amount = _decimal(raw.amount)
            currency = _stored_currency_code(raw.currency)
            if amount < 0 or not currency or currency != account.currency:
                raise ValueError("Stored account spend is invalid.")
            account_daily[account.name][str(raw.spend_date)] += amount
            account_totals.setdefault(account.name, {"currency": currency, "amount": Decimal(0)})["amount"] += amount
        except (ValueError, InvalidOperation, TypeError):
            unreadable_count += 1

    ad_daily = defaultdict(lambda: defaultdict(Decimal))
    for raw in ad_rows:
        account = account_by_name.get(raw.ad_account)
        run = runs_by_account.get(raw.ad_account)
        if not account or not run or raw.sync_run != run.name or raw.external_account_id != account.external_account_id:
            unreadable_count += 1
            continue
        try:
            amount = _decimal(raw.amount)
            currency = _stored_currency_code(raw.currency)
            if amount < 0 or not currency or currency != account.currency or not raw.ad_id:
                raise ValueError("Stored ad spend is invalid.")
            ad_daily[account.name][str(raw.spend_date)] += amount
            asset = _read_attributed_page(raw)
            attribution_status = raw.attribution_status or "Unmapped"
            page_key = _page_group_key(account.name, attribution_status, asset, raw.page_id, raw.instagram_actor_id)
            if page_key not in page_groups:
                page_groups[page_key] = {
                    "key": page_key,
                    "account": account.name,
                    "account_name": account.account_name,
                    "external_account_id": account.external_account_id,
                    "billing_profile": raw.billing_profile or "",
                    "customer": raw.billing_customer_snapshot or "",
                    "attribution_status": attribution_status,
                    "page_id": (asset or {}).get("page_id") or raw.page_id or "",
                    "page_name": (asset or {}).get("page_name") or raw.page_name or "",
                    "instagram_account_id": (asset or {}).get("instagram_account_id") or raw.instagram_actor_id or "",
                    "instagram_username": (asset or {}).get("instagram_username") or "",
                    "served_customer": (asset or {}).get("served_customer") or "",
                    "currency": currency,
                    "spend": Decimal(0),
                    "campaign_count": 0,
                    "ad_count": 0,
                    "daily_spend": defaultdict(Decimal),
                }
            page_groups[page_key]["spend"] += amount
            if attribution_status != "Attributed":
                page_groups[page_key]["daily_spend"][str(raw.spend_date)] += amount
            campaign_key = "|".join((page_key, raw.campaign_id or "", currency))
            if campaign_key not in campaign_groups:
                campaign_groups[campaign_key] = {
                    "key": campaign_key,
                    "page_key": page_key,
                    "account": account.name,
                    "campaign_id": raw.campaign_id or "",
                    "campaign_name": raw.campaign_name or raw.campaign_id or "",
                    "currency": currency,
                    "spend": Decimal(0),
                    "ad_ids": set(),
                    "daily_spend": defaultdict(Decimal),
                }
            campaign_groups[campaign_key]["spend"] += amount
            if attribution_status != "Attributed":
                campaign_groups[campaign_key]["daily_spend"][str(raw.spend_date)] += amount
            if raw.ad_id:
                campaign_groups[campaign_key]["ad_ids"].add(raw.ad_id)
                ad_ids_by_group[page_key].add(raw.ad_id)
        except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
            unreadable_count += 1

    for campaign in campaign_groups.values():
        page_groups[campaign["page_key"]]["campaign_count"] += 1
    for page_key, ad_ids in ad_ids_by_group.items():
        page_groups[page_key]["ad_count"] = len(ad_ids)

    reconciliation = []
    all_accounts_ready = bool(account_rows) and not accounts_truncated and not runs_truncated
    for account in account_rows:
        run = runs_by_account.get(account.name)
        ready = bool(run and run.ad_insights_status == "Complete")
        if not ready:
            all_accounts_ready = False
        source_precision = _currency_precision(account.currency)
        daily_differences = {}
        total_difference = Decimal(0)
        dates = set(account_daily[account.name]) | set(ad_daily[account.name])
        if source_precision is None:
            unreadable_count += 1
            all_accounts_ready = False
        else:
            for spend_date in dates:
                difference = _quantize(
                    account_daily[account.name].get(spend_date, Decimal(0))
                    - ad_daily[account.name].get(spend_date, Decimal(0)),
                    source_precision,
                )
                if difference:
                    daily_differences[spend_date] = format(difference, "f")
                    total_difference += difference
        matched = ready and not daily_differences
        reconciliation.append({
            "account": account.name,
            "account_name": account.account_name,
            "external_account_id": account.external_account_id,
            "currency": account.currency,
            "account_spend": format(account_totals.get(account.name, {}).get("amount", Decimal(0)), "f"),
            "ad_spend": format(sum(ad_daily[account.name].values(), Decimal(0)), "f"),
            "difference": format(total_difference, "f"),
            "status": "Matched" if matched else ("Mismatch" if ready else ("Failed" if run else "Not Synced")),
            "ad_insights_status": run.ad_insights_status if run else "Not Fetched",
            "ad_row_count": int(run.ad_row_count or 0) if run else 0,
            "mismatched_day_count": len(daily_differences),
            "message": run.ad_message or "" if run else "",
            "daily_differences": daily_differences,
        })

    complete = all_accounts_ready and not rows_truncated and unreadable_count == 0
    # The Pages of the SWITCHED-OFF arrangements. Their spend is assigned —
    # the payer is named — and deliberately not charged, so it must not land
    # in the unassigned bucket beside spend nobody has mapped.
    non_billable_assets = _non_billable_page_assets(company_name) if ad_rows else []
    # The ad-level delivery total, per currency, BEFORE any of it is placed.
    ad_totals = defaultdict(Decimal)
    for account in account_by_name.values():
        for amount in ad_daily.get(account.name, {}).values():
            ad_totals[account.currency] += amount
    # The six buckets, accumulated while the spend is still an exact Decimal.
    chargeability_buckets = {
        state: defaultdict(Decimal) for state in chargeability.CHARGEABILITY_STATES
    }
    pages = []
    for page in page_groups.values():
        switched_off_asset = None
        if page["attribution_status"] != chargeability.ATTRIBUTION_STATUS_ATTRIBUTED:
            switched_off_asset = _match_saved_page(
                {
                    "page_id": page.get("page_id") or "",
                    "instagram_actor_id": page.get("instagram_account_id") or "",
                },
                non_billable_assets,
            )
        state, reason = chargeability.classify_chargeability({
            "attribution_status": page["attribution_status"],
            "switched_off": switched_off_asset is not None,
        })
        page["chargeability"] = state
        page["chargeability_reason"] = reason
        chargeability_buckets[state][page["currency"]] += page["spend"]
        page["spend"] = format(page["spend"], "f")
        daily_spend = page.pop("daily_spend", {})
        page["daily_spend"] = [
            {
                "spend_date": spend_date,
                "spend": format(amount, "f"),
                "currency": page["currency"],
            }
            for spend_date, amount in sorted(daily_spend.items())
        ] if page["attribution_status"] != "Attributed" else []
        # A switched-off arrangement still NAMES its customer, so the reader
        # can see whose spend it is and why it is not being charged.
        profile = (switched_off_asset or {}).get("_billing_profile") or {}
        page["chargeability_customer"] = profile.get("customer_name") or ""
        page["chargeability_profile"] = profile.get("name") or ""
        pages.append(page)
    campaigns = []
    for campaign in campaign_groups.values():
        campaign["spend"] = format(campaign["spend"], "f")
        campaign["ad_count"] = len(campaign.pop("ad_ids"))
        daily_spend = campaign.pop("daily_spend", {})
        campaign["daily_spend"] = [
            {
                "spend_date": spend_date,
                "spend": format(amount, "f"),
                "currency": campaign["currency"],
            }
            for spend_date, amount in sorted(daily_spend.items())
        ] if page_groups[campaign["page_key"]]["attribution_status"] != "Attributed" else []
        # A campaign belongs to a Page, so it inherits the Page's state. A
        # campaign that reported a state of its own would be able to disagree
        # with the Page it sits under.
        parent_page = page_groups[campaign["page_key"]]
        campaign["chargeability"] = parent_page["chargeability"]
        campaign["chargeability_reason"] = parent_page["chargeability_reason"]
        campaigns.append(campaign)
    return {
        "company": company_name,
        "since": start.isoformat(),
        "until": end.isoformat(),
        "complete": complete,
        "truncated": accounts_truncated or runs_truncated or rows_truncated,
        "unreadable_count": unreadable_count,
        "accounts": reconciliation,
        # TRACK B — the attribution reconciliation, against the six states.
        "attribution_reconciliation": _attribution_reconciliation(
            chargeability_buckets,
            ad_totals,
            unreadable_count,
            accounts_truncated or runs_truncated or rows_truncated,
            all_accounts_ready,
        ),
        "pages": sorted(pages, key=lambda row: (row["page_name"].casefold(), row["account_name"].casefold(), row["currency"])),
        "campaigns": sorted(campaigns, key=lambda row: (row["campaign_name"].casefold(), row["account"], row["currency"])),
        "source_label": META_SOURCE,
        "attribution_label": "Ad-level Insights joined to the ad creative Page identity",
    }


def _discover_accounts(version, token):
    data = []
    after = None
    pages = 0
    while True:
        params = {"fields": "id,name,account_id,currency,timezone_name,account_status", "limit": 100}
        if after:
            params["after"] = after
        response = _meta_get(version, "me/adaccounts", params, token)
        batch = response.get("data")
        if not isinstance(batch, list):
            raise ValueError("Meta account discovery returned an incomplete response.")
        data.extend(batch)
        pages += 1
        paging = response.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        has_next = bool(paging.get("next"))
        if not has_next:
            return data, True
        if not after or pages >= MAX_ACCOUNT_DISCOVERY_PAGES:
            return data, False


def _read_daily_spend(version, token, account_id, since, until):
    data = {}
    after = None
    pages = 0
    while True:
        params = {
            "fields": "spend,date_start,date_stop",
            "level": "account",
            "time_increment": 1,
            "time_range": json.dumps({"since": since, "until": until}, separators=(",", ":")),
            "limit": 500,
        }
        if after:
            params["after"] = after
        response = _meta_get(version, f"{account_id}/insights", params, token)
        batch = response.get("data")
        if not isinstance(batch, list):
            raise ValueError("Meta Insights did not return a daily data list.")
        for row in batch:
            date_text = row.get("date_start")
            if not isinstance(date_text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
                raise ValueError("Meta Insights returned an invalid date.")
            if not (since <= date_text <= until):
                raise ValueError("Meta Insights returned spend outside the requested period.")
            amount = _decimal(row.get("spend"))
            if amount < 0:
                raise ValueError("Meta Insights returned a negative spend amount.")
            data[date_text] = data.get(date_text, Decimal(0)) + amount

        pages += 1
        paging = response.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        has_next = bool(paging.get("next"))
        if not has_next:
            return data
        if not after or pages >= MAX_INSIGHT_PAGES:
            raise ValueError("Meta Insights pagination did not complete.")


def _read_daily_ad_spend(version, token, account_id, since, until):
    """Read daily costs by ad and join each ad to its creative identity."""
    insight_rows = {}
    after = None
    pages = 0
    while True:
        params = {
            "fields": "spend,date_start,date_stop,account_id,account_name,campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name",
            "level": "ad",
            "time_increment": 1,
            "time_range": json.dumps({"since": since, "until": until}, separators=(",", ":")),
            "limit": 500,
        }
        if after:
            params["after"] = after
        response = _meta_get(version, f"{account_id}/insights", params, token)
        batch = response.get("data")
        if not isinstance(batch, list):
            raise ValueError("Meta ad-level Insights did not return a daily data list.")
        for raw in batch:
            spend_date = raw.get("date_start")
            ad_id = raw.get("ad_id")
            if not isinstance(spend_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", spend_date):
                raise ValueError("Meta ad-level Insights returned an invalid date.")
            if not (since <= spend_date <= until):
                raise ValueError("Meta ad-level Insights returned spend outside the requested period.")
            if not isinstance(ad_id, (str, int)) or not str(ad_id).isdigit():
                raise ValueError("Meta ad-level Insights returned an invalid ad ID.")
            amount = _decimal(raw.get("spend"))
            if amount < 0:
                raise ValueError("Meta ad-level Insights returned a negative spend amount.")
            key = (spend_date, str(ad_id))
            row = insight_rows.setdefault(key, {
                "spend_date": spend_date,
                "ad_id": str(ad_id),
                "ad_name": _text(raw.get("ad_name") or "", "Meta ad name", 140),
                "adset_id": _text(raw.get("adset_id") or "", "Meta ad set ID", 100),
                "adset_name": _text(raw.get("adset_name") or "", "Meta ad set name", 140),
                "campaign_id": _text(raw.get("campaign_id") or "", "Meta campaign ID", 100),
                "campaign_name": _text(raw.get("campaign_name") or "", "Meta campaign name", 140),
                "amount": Decimal(0),
            })
            if any(row[field] != value for field, value in (
                ("ad_name", _text(raw.get("ad_name") or "", "Meta ad name", 140)),
                ("adset_id", _text(raw.get("adset_id") or "", "Meta ad set ID", 100)),
                ("campaign_id", _text(raw.get("campaign_id") or "", "Meta campaign ID", 100)),
            )):
                raise ValueError("Meta returned conflicting identity for an ad-level Insights row.")
            row["amount"] += amount
            if len(insight_rows) > MAX_AD_INSIGHT_ROWS:
                raise ValueError("Meta returned more ad-level Insights rows than this sync can safely process.")

        pages += 1
        paging = response.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        has_next = bool(paging.get("next"))
        if not has_next:
            break
        if not after or pages >= MAX_INSIGHT_PAGES:
            raise ValueError("Meta ad-level Insights pagination did not complete.")

    metadata = _read_ad_metadata(version, token, account_id)
    result = []
    for (spend_date, ad_id), row in sorted(insight_rows.items()):
        ad = metadata.get(ad_id, {})
        creative = ad.get("creative") if isinstance(ad.get("creative"), dict) else {}
        object_story_spec = creative.get("object_story_spec") if isinstance(creative.get("object_story_spec"), dict) else {}
        effective_story_id = _text(
            creative.get("effective_object_story_id") or creative.get("object_story_id") or "",
            "Meta effective story ID", 140,
        )
        explicit_page_id = _text(object_story_spec.get("page_id") or "", "Meta Page ID", 100)
        story_page_id = _page_id_from_story_id(effective_story_id)
        result.append({
            **row,
            "creative_id": _text(creative.get("id") or "", "Meta creative ID", 100),
            "creative_story_id": effective_story_id,
            "page_id": explicit_page_id or story_page_id,
            "instagram_actor_id": _text(creative.get("instagram_actor_id") or "", "Meta Instagram account ID", 100),
        })
    return result


def _read_ad_metadata(version, token, account_id):
    """Read all account ads and their creative Page/Instagram identity."""
    metadata = {}
    after = None
    pages = 0
    while True:
        params = {
            "fields": "id,name,creative{id,object_story_spec,object_story_id,instagram_actor_id,effective_object_story_id}",
            "limit": 500,
        }
        if after:
            params["after"] = after
        response = _meta_get(version, f"{account_id}/ads", params, token)
        batch = response.get("data")
        if not isinstance(batch, list):
            raise ValueError("Meta ad discovery did not return an ad list.")
        for raw in batch:
            ad_id = raw.get("id")
            if not isinstance(ad_id, (str, int)) or not str(ad_id).isdigit():
                raise ValueError("Meta ad discovery returned an invalid ad ID.")
            creative = raw.get("creative")
            if creative is not None and not isinstance(creative, dict):
                raise ValueError("Meta ad discovery returned invalid creative details.")
            metadata[str(ad_id)] = {"creative": creative or {}}
            if len(metadata) > MAX_ADS:
                raise ValueError("This Meta account has more ads than one page-level sync can safely process.")
        pages += 1
        paging = response.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        has_next = bool(paging.get("next"))
        if not has_next:
            return metadata
        if not after or pages >= MAX_AD_METADATA_PAGES:
            raise ValueError("Meta ad metadata pagination did not complete.")


def _attribute_ad_page(ad, page_assets):
    explicit_page_id = ad.get("page_id") or ""
    effective_story_page_id = _page_id_from_story_id(
        ad.get("effective_object_story_id") or ad.get("creative_story_id") or ""
    )
    page_id = explicit_page_id or effective_story_page_id
    instagram_id = ad.get("instagram_actor_id") or ""
    page_matches = [asset for asset in page_assets if page_id and asset.get("page_id") == page_id]
    instagram_matches = [asset for asset in page_assets if instagram_id and asset.get("instagram_account_id") == instagram_id]
    asset = None
    status = "Unmapped"
    if explicit_page_id and effective_story_page_id and explicit_page_id != effective_story_page_id:
        status = "Ambiguous"
    elif len(page_matches) > 1 or len(instagram_matches) > 1:
        status = "Ambiguous"
    elif page_matches and instagram_matches and page_matches[0] != instagram_matches[0]:
        status = "Ambiguous"
    elif page_matches:
        asset = page_matches[0]
        status = "Attributed"
    elif instagram_matches and not page_id:
        asset = instagram_matches[0]
        status = "Attributed"
    return {
        "status": status,
        "page_id": page_id,
        "page_name": asset.get("page_name", "") if asset else "",
        "instagram_actor_id": instagram_id,
        "asset": asset,
    }


def _page_id_from_story_id(value):
    """Return the Facebook Page prefix from Meta's ``page_id_post_id`` ID."""
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(r"(\d+)_(\d+)", value.strip())
    return match.group(1) if match else ""


def _match_saved_page(identity, page_assets):
    """Match a STORED Page identity against a set of saved assets.

    The same rules `_attribute_ad_page` applies, without the creative-story
    prefix — because a stored row's `page_id` already carries the RESOLVED
    identity, and the row's status already carries the conflict verdict.

    A conflict is NOT a match. Two assets claiming the same Page is a decision
    a human has to make, and this helper will not make it by picking one.
    """
    page_id = (identity or {}).get("page_id") or ""
    instagram_id = (identity or {}).get("instagram_actor_id") or ""
    page_matches = [asset for asset in page_assets if page_id and asset.get("page_id") == page_id]
    instagram_matches = [
        asset for asset in page_assets
        if instagram_id and asset.get("instagram_account_id") == instagram_id
    ]
    if len(page_matches) > 1 or len(instagram_matches) > 1:
        return None
    if page_matches and instagram_matches and page_matches[0] != instagram_matches[0]:
        return None
    if page_matches:
        return page_matches[0]
    if instagram_matches and not page_id:
        return instagram_matches[0]
    return None


def _collect_non_billable_spend(ad_rows, account_by_name, run_by_account, non_billable_assets):
    """The spend of ads whose Page belongs to a SWITCHED-OFF arrangement.

    These rows are not charged — that is what "disabled" means — and they are
    not unassigned either: the Page is known, and a payer is named. They are
    reported in their OWN bucket, so the two are never added together and
    never read as one number.

    Only rows the sync stored as UNMAPPED are considered. A row that matched an
    enabled profile was charged, and a row that was ambiguous needs a human
    decision — neither is a switched-off arrangement, and neither may be moved
    into this bucket.
    """
    totals = defaultdict(Decimal)
    matched_pages = set()
    for raw in ad_rows:
        account = account_by_name.get(raw.ad_account)
        run = run_by_account.get(raw.ad_account)
        if (
            not account or not run or raw.level != "Ad" or raw.sync_run != run.name or
            raw.external_account_id != account.external_account_id
        ):
            continue
        if (raw.attribution_status or "Unmapped") != "Unmapped":
            continue
        try:
            amount = _decimal(raw.amount)
            currency = _stored_currency_code(raw.currency)
            if amount < 0 or not currency or currency != account.currency or not raw.ad_id:
                raise ValueError("Stored ad spend is invalid.")
        except (InvalidOperation, TypeError, ValueError):
            continue
        asset = _match_saved_page(
            {"page_id": raw.page_id, "instagram_actor_id": raw.instagram_actor_id},
            non_billable_assets,
        )
        if not asset:
            continue
        totals[currency] += amount
        matched_pages.add((raw.ad_account, raw.page_id or raw.instagram_actor_id or ""))
    return totals, matched_pages


def _reconcile_ad_spend(account_daily, ad_rows, currency):
    precision = _currency_precision(currency)
    if precision is None:
        raise ValueError("Meta account currency precision is unavailable.")
    ad_daily = defaultdict(Decimal)
    for row in ad_rows:
        ad_daily[row["spend_date"]] += row["amount"]
    differences = []
    for spend_date in sorted(set(account_daily) | set(ad_daily)):
        difference = _quantize(account_daily.get(spend_date, Decimal(0)) - ad_daily.get(spend_date, Decimal(0)), precision)
        if difference:
            differences.append(difference)
    total_difference = _quantize(sum(differences, Decimal(0)), precision)
    return ("Mismatch" if differences else "Matched"), format(total_difference, "f"), len(differences)


def _read_attributed_page(row):
    if row.attribution_status != "Attributed":
        return None
    try:
        value = json.loads(row.page_asset_snapshot or "")
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _page_group_key(account_name, status, asset, page_id, instagram_id):
    if asset:
        return "|".join((account_name, "Attributed", asset.get("page_id", ""), asset.get("instagram_account_id", "")))
    return "|".join((account_name, status, page_id or "", instagram_id or "", "unknown"))


def _meta_get(version, path, params, token):
    url = f"https://graph.facebook.com/{version}/{path}"
    try:
        response = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=META_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as error:
        raise ValueError("Meta could not be reached.") from error
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"Meta API returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("Meta returned an unreadable response.") from error
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("Meta rejected the request.")
    return payload


def _parse_page_assets(value):
    try:
        assets = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        frappe.throw("Page mappings must be valid JSON rows.")
    if not isinstance(assets, list) or len(assets) > 100:
        frappe.throw("Provide between zero and 100 Page mappings.")
    cleaned = []
    for asset in assets:
        if not isinstance(asset, dict):
            frappe.throw("Each Page mapping must be an object.")
        allowed = {"page_id", "page_name", "instagram_account_id", "instagram_username", "served_customer"}
        if set(asset) - allowed:
            frappe.throw("A Page mapping contains unsupported fields.")
        page_id = _text(asset.get("page_id"), "Facebook Page ID", 100)
        page_name = _text(asset.get("page_name"), "Facebook Page name", 140)
        if not page_id or not page_name:
            frappe.throw("Each Page mapping needs its ID and name.")
        served = _text(asset.get("served_customer"), "Customer served", 140)
        if served:
            customer = frappe.get_doc("Customer", served)
            customer.check_permission("read")
            if customer.disabled:
                frappe.throw("Choose an enabled Customer for the Page mapping.")
        cleaned.append({
            "page_id": page_id,
            "page_name": page_name,
            "instagram_account_id": _text(asset.get("instagram_account_id"), "Instagram account ID", 100),
            "instagram_username": _text(asset.get("instagram_username"), "Instagram username", 140),
            "served_customer": served,
        })
    return cleaned


def _parse_fee_rules(value):
    """Validate a profile's date-ordered percentage changes."""
    try:
        rules = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        frappe.throw("Fee rules must be valid JSON rows.")
    if rules is None:
        return []
    if not isinstance(rules, list) or len(rules) > 500:
        frappe.throw("Provide no more than 500 dated fee rules.")

    normalized = []
    seen_dates = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"effective_from", "fee_percent"}:
            frappe.throw("Each fee rule must contain only an effective date and percentage.")
        effective_from = _text(rule.get("effective_from"), "Fee effective date", 10)
        raw_fee = rule.get("fee_percent")
        if not effective_from or raw_fee in (None, ""):
            frappe.throw("Each dated fee rule needs an effective date and percentage.")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", effective_from):
            frappe.throw("Fee effective dates must use YYYY-MM-DD format.")
        try:
            effective_date = getdate(effective_from)
            fee = Decimal(str(raw_fee))
        except (InvalidOperation, TypeError, ValueError):
            frappe.throw("Enter a valid fee date and percentage.")
        if effective_date.isoformat() != effective_from:
            frappe.throw("Fee effective dates must be valid calendar dates.")
        if not fee.is_finite() or fee < 0 or fee > 100:
            frappe.throw("Service fee must be between 0% and 100%.")
        if effective_from in seen_dates:
            frappe.throw("A billing profile can have only one fee rule per effective date.")
        seen_dates.add(effective_from)
        normalized.append({"effective_from": effective_from, "fee_percent": format(fee, "f")})
    return sorted(normalized, key=lambda rule: rule["effective_from"])


def _fee_status_for_date(profile, on_date):
    """The fee for a spend date, AND whether anybody ever stated it.

    Returns (status, percent_text). The status is `configured` when a fee was
    written down — by a dated rule, or by a standing fee that is not zero, or
    by the profile's explicit `fee_is_configured` flag — and `unconfigured`
    when nobody said what the fee is.

    WHY THE STATUS IS SNAPSHOTTED, NOT READ BACK LATER
    --------------------------------------------------

    The percent alone cannot tell an APPROVED ZERO from NO FEE AT ALL, and the
    distinction has to survive the profile being edited later. So the sync
    records the STATUS on the spend row, next to the percent it applied, and
    the invoice reads that snapshot. A profile whose fee is changed tomorrow
    must not change what yesterday's spend was invoiced at.

    See `meta_ads_invoicing.resolve_fee` for the rule itself.
    """
    if not profile:
        return invoicing.FEE_STATUS_UNCONFIGURED, "0"
    try:
        target_date = getdate(on_date)
    except Exception as error:
        raise ValueError("Spend date is invalid for fee-rule lookup.") from error
    rules = []
    seen_dates = set()
    for rule in profile.get("fee_rules") or []:
        try:
            effective_date = getdate(rule.get("effective_from"))
            fee = Decimal(str(rule.get("fee_percent") or "0"))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Stored fee rule is invalid.") from error
        if not fee.is_finite() or fee < 0 or fee > 100:
            raise ValueError("Stored fee rule is outside the supported percentage range.")
        if effective_date in seen_dates:
            raise ValueError("Stored fee rules contain a duplicate effective date.")
        seen_dates.add(effective_date)
        rules.append({"effective_from": effective_date, "fee_percent": fee})
    status, percent, _source = invoicing.resolve_fee(
        rules,
        profile.get("fee_percent") or "0",
        bool(profile.get("fee_is_configured")),
        target_date,
    )
    return status, format(percent, "f")


def _fee_percent_for_date(profile, on_date):
    """Use the latest fee rule effective on the spend date, or the base fee."""
    _status, percent = _fee_status_for_date(profile, on_date)
    return percent


def _snapshot_page_assets(value):
    try:
        assets = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(assets, list):
        return []
    return [
        {
            "page_id": _text(asset.get("page_id"), "Facebook Page ID", 100),
            "page_name": _text(asset.get("page_name"), "Facebook Page name", 140),
            "instagram_account_id": _text(asset.get("instagram_account_id"), "Instagram account ID", 100),
            "instagram_username": _text(asset.get("instagram_username"), "Instagram username", 140),
            "served_customer": _text(asset.get("served_customer"), "Customer served", 140),
        }
        for asset in assets if isinstance(asset, dict)
    ]


def _profile_policies(company_name, profile_names):
    """The POLICY each named profile runs under, keyed by profile name.

    Only the fields that answer a policy question are read. Everything else
    about the profile — the fee, the Page assets — is already on the spend
    snapshot, and reading it again here would be a second answer to the same
    question.
    """
    names = [name for name in (profile_names or set()) if name]
    if not names:
        return {}
    rows = frappe.get_all(
        "LaraTik Meta Ads Billing Profile",
        filters={"name": ["in", names]},
        fields=[
            "name", "fee_is_configured", "charge_model", "recognition_model",
            "income_account", "invoice_item", "tax_treatment", "tax_template",
        ],
        limit_page_length=len(names) + 1,
    )
    return {
        row.name: {
            "fee_is_configured": bool(row.fee_is_configured),
            "charge_model": row.charge_model or "",
            "recognition_model": row.recognition_model or "",
            "income_account": row.income_account or "",
            "invoice_item": row.invoice_item or "",
            "tax_treatment": row.tax_treatment or "",
            "tax_template": row.tax_template or "",
        }
        for row in rows
    }


def _profile_payloads(company_name):
    profiles = frappe.get_all(
        "LaraTik Meta Ads Billing Profile",
        filters={"company": company_name},
        fields=[
            "name", "customer", "relationship_kind", "fee_percent", "invoice_currency",
            "disabled", "fee_is_configured", "charge_model", "recognition_model",
            "income_account", "invoice_item", "tax_treatment", "tax_template",
        ],
        order_by="modified desc",
        limit_page_length=MAX_BILLING_PROFILES + 1,
    )
    return [_profile_payload(row) for row in profiles]


def _page_profile_assets(profiles):
    """Attach each saved Page asset to the payer profile that owns its mapping."""
    assets = []
    for profile in profiles:
        if profile.get("disabled"):
            continue
        for asset in profile.get("page_assets") or []:
            assets.append({**asset, "_billing_profile": profile})
    return assets


def _non_billable_page_assets(company_name):
    """The Page assets of the SWITCHED-OFF payer arrangements.

    ─── WHY A DISABLED PROFILE'S PAGES ARE STILL KNOWN ───────────────────────────

    A disabled profile is an ADMINISTRATIVE DECISION: this payer arrangement is
    switched off, so its Pages are not charged. It is NOT a statement that
    nobody owns those Pages.

    Before, the disabled profile's assets were dropped from the asset list
    entirely, so its spend came back as UNASSIGNED — the same bucket as spend
    whose Page nobody has ever mapped. Those two are not the same thing: one
    is a decision, the other is a gap, and the brief is explicit that they must
    not be treated as financially equivalent.

    So the assets are kept, marked, and reported as "assigned but not
    billable" — with the customer named, so the reader can see whose spend it
    is and WHY it is not being charged.

    The read is deliberately cheap and narrow: the DISABLED profiles only, and
    their Page assets. It does not load the whole profile set, because this
    runs on every report read and a report does not need the fee schedules.

    It is THREE queries, not one per profile. A report that reads a hundred
    disabled arrangements must not issue two hundred queries to say what is
    switched off, so the profiles, their Page assets and their customer names
    are each fetched once.
    """
    profiles = frappe.get_all(
        "LaraTik Meta Ads Billing Profile",
        filters={"company": company_name, "disabled": 1},
        fields=["name", "customer"],
        order_by="name asc",
        limit_page_length=MAX_BILLING_PROFILES + 1,
    )
    profiles = profiles[:MAX_BILLING_PROFILES]
    if not profiles:
        return []

    customer_names = {}
    customer_ids = sorted({profile.customer for profile in profiles if profile.customer})
    if customer_ids:
        for row in frappe.get_all(
            "Customer",
            filters={"name": ["in", customer_ids]},
            fields=["name", "customer_name"],
            limit_page_length=len(customer_ids),
        ):
            customer_names[row.name] = row.customer_name or row.name

    assets = []
    profile_by_name = {profile.name: profile for profile in profiles}
    for asset in frappe.get_all(
        "LaraTik Meta Ads Page Asset",
        filters={
            "parent": ["in", list(profile_by_name)],
            "parenttype": "LaraTik Meta Ads Billing Profile",
        },
        fields=[
            "parent", "page_id", "page_name", "instagram_account_id", "instagram_username",
            "served_customer",
        ],
        limit_page_length=MAX_BILLING_PROFILES * 20,
    ):
        profile = profile_by_name.get(asset.parent)
        if not profile:
            continue
        customer = profile.customer or ""
        assets.append({
            **asset,
            "_billing_profile": {
                "name": profile.name,
                "customer": customer,
                "customer_name": customer_names.get(customer) or customer,
            },
        })
    return assets


def _profile_payload(profile_row):
    profile = frappe.get_doc("LaraTik Meta Ads Billing Profile", profile_row.name)
    profile.check_permission("read")
    customer_name = frappe.db.get_value("Customer", profile.customer, "customer_name") if profile.customer else ""
    return {
        "name": profile.name,
        "customer": profile.customer or "",
        "customer_name": customer_name or profile.customer or "",
        "relationship_kind": profile.relationship_kind or "Customer",
        "fee_percent": _decimal_text(profile.fee_percent or "0"),
        # THE POLICY ANSWERS. Each one is a decision somebody has to make, and
        # each one is UNSET by default — the app never picks one. An unset
        # field is "not decided", which blocks the draft and leaves the
        # workspace and the preview fully usable.
        #
        # `fee_is_configured` is a NUMBER, not a boolean, because the form
        # holds a Check, and the web app reads the flag the way a Check reads:
        # 1 or 0.
        "fee_is_configured": int(bool(profile.fee_is_configured)),
        "charge_model": profile.charge_model or "",
        "recognition_model": profile.recognition_model or "",
        "income_account": profile.income_account or "",
        "invoice_item": profile.invoice_item or "",
        "tax_treatment": profile.tax_treatment or "",
        "tax_template": profile.tax_template or "",
        "fee_rules": [{
            "effective_from": str(row.effective_from),
            "fee_percent": _decimal_text(row.fee_percent or "0"),
        } for row in sorted(profile.get("fee_rules") or [], key=lambda item: str(item.effective_from))],
        "invoice_currency": profile.invoice_currency or "",
        "disabled": bool(profile.disabled),
        "page_assets": [{
            "page_id": row.page_id or "",
            "page_name": row.page_name or "",
            "instagram_account_id": row.instagram_account_id or "",
            "instagram_username": row.instagram_username or "",
            "served_customer": row.served_customer or "",
        } for row in profile.get("page_assets") or []],
    }


def _selling_rate(source_currency, invoice_currency, on_date):
    if not source_currency or not invoice_currency:
        return None, None
    if source_currency == invoice_currency:
        return Decimal(1), on_date.isoformat()
    rows = frappe.get_all(
        "Currency Exchange",
        filters={
            "from_currency": source_currency,
            "to_currency": invoice_currency,
            "date": ["<=", on_date.isoformat()],
            "for_selling": 1,
        },
        fields=["date", "exchange_rate"],
        order_by="date desc, name desc",
        limit_page_length=1,
    )
    if not rows:
        return None, None
    try:
        rate = _decimal(rows[0].exchange_rate)
    except (InvalidOperation, TypeError, ValueError):
        return None, None
    if rate <= 0:
        return None, None
    return rate, str(rows[0].date)


def _currency_precision(currency):
    if not currency:
        return None
    units = frappe.db.get_value("Currency", currency, "fraction_units")
    try:
        units = int(units)
    except (TypeError, ValueError):
        return None
    if units < 1:
        return None
    places = 0
    while units > 1 and units % 10 == 0:
        units //= 10
        places += 1
    return places if units == 1 else None


def _quantize(value, places):
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _connection(company_name, connection_name):
    name = _text(connection_name, "Meta connection", 140)
    connection = frappe.get_doc("LaraTik Meta Ads Connection", name)
    connection.check_permission("read")
    if connection.company != company_name or connection.status != "Connected":
        frappe.throw("Choose a connected Meta account for this Company.")
    return connection


def _token(connection):
    token = connection.get_password("access_token")
    if not token:
        frappe.throw("The Meta access token is not available. Reconnect this account.")
    return token


def _company_name(value):
    company_name = _text(value, "Company", 140)
    if not company_name or not frappe.db.exists("Company", company_name):
        frappe.throw("Choose an existing Company.")
    frappe.get_doc("Company", company_name).check_permission("read")
    return company_name


def _date_range(since, until):
    since_text = _text(since, "Start date", 10)
    until_text = _text(until, "End date", 10)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since_text) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until_text):
        frappe.throw("Choose dates in YYYY-MM-DD format.")
    try:
        start = getdate(since_text)
        end = getdate(until_text)
    except Exception:
        frappe.throw("The selected dates are not valid.")
    if start > end:
        frappe.throw("The start date must be on or before the end date.")
    if (end - start).days >= MAX_DAYS:
        frappe.throw(f"Choose a period of no more than {MAX_DAYS} days.")
    return start, end


def _ad_account_id(raw):
    value = raw.get("id") or raw.get("account_id")
    if isinstance(value, int):
        value = f"act_{value}"
    if isinstance(value, str) and value.isdigit():
        value = f"act_{value}"
    if not isinstance(value, str) or not re.fullmatch(r"act_\d+", value):
        frappe.throw("Meta returned an invalid ad account ID.")
    if not raw.get("currency"):
        frappe.throw("Meta returned an ad account without its currency.")
    return value


def _normalize_ad_account_id(value):
    account_id = _text(value, "Meta ad account ID", 32)
    if account_id.isdigit():
        account_id = f"act_{account_id}"
    if not re.fullmatch(r"act_\d+", account_id, flags=re.IGNORECASE):
        frappe.throw("Enter a numeric Meta ad account ID or an act_ account ID.")
    return "act_" + account_id[4:]


def _currency_code(value):
    code = _text(value, "Currency", 3).upper()
    if not re.fullmatch(r"[A-Z]{3}", code) or not frappe.db.exists("Currency", code):
        frappe.throw("Meta returned a currency that ERPNext does not recognize.")
    return code


def _stored_currency_code(value):
    """The currency code of a value that was VALIDATED WHEN IT WAS SAVED.

    ─── WHY A STORED VALUE IS READ DIFFERENTLY ──────────────────────────────────

    `_currency_code` THROWS for a currency ERPNext does not recognise. That is
    right for a value that has just arrived from Meta — it is a fresh input and
    a fresh input should be refused loudly.

    It is wrong for a value being READ BACK. Every read path reads a stored
    snapshot inside a `try` that catches `ValueError`, `TypeError` and
    `InvalidOperation` — and Frappe's own validation error is none of those,
    so a snapshot whose currency has since been disabled took the whole report
    down with a 500 instead of being counted as an unreadable row.

    And a stored snapshot with NO billing currency at all — a payer
    arrangement that never had one — is not an error either. It is a charge
    that cannot be invoiced yet, and the honest answer is a BLOCKED line the
    reader can act on, not a dead screen.

    So a stored currency is read TOLERANTLY, and the caller decides what an
    unusable one means.
    """
    code = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        return ""
    if not frappe.db.exists("Currency", code):
        return ""
    return code


def _text(value, label, maximum):
    if value is None:
        return ""
    if not isinstance(value, (str, int)):
        frappe.throw(f"{label} must be text.")
    result = str(value).strip()
    if len(result) > maximum:
        frappe.throw(f"{label} is too long.")
    return result


def _policy_choice(value, label, vocabulary):
    """The reader's answer to ONE policy question, or None for "not answered".

    ─── WHY THIS IS THREE STATES AND NOT TWO ───────────────────────────────────

    `None` means the caller did not answer this question at all, and the
    stored answer must not change. `""` means the reader chose "not decided",
    which CLEARS the answer. A value outside `vocabulary` is refused.

    The distinction is the difference between "leave my other answers alone"
    and "take this one back", and a caller that cannot tell them apart cannot
    offer the reader either.
    """
    if value is None:
        return None
    answer = _text(value, label, 60)
    if not answer:
        return ""
    if answer not in vocabulary:
        frappe.throw(f"Choose a {label.lower()} from the list.")
    return answer


def _check_flag(value):
    """A Check field's answer: 1, 0, True or False — and nothing else."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    text = _text(value, "Fee flag", 8).casefold()
    if text in ("1", "true", "yes", "on"):
        return 1
    if text in ("", "0", "false", "no", "off"):
        return 0
    frappe.throw("The fee flag must be true or false.")


def _decimal(value):
    if not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Amount must be a decimal string.")
    raw = str(value).strip()
    if not DECIMAL_PATTERN.fullmatch(raw):
        raise ValueError("Amount must be a non-negative decimal.")
    result = Decimal(raw)
    if not result.is_finite():
        raise ValueError("Amount must be finite.")
    return result


def _decimal_text(value):
    try:
        return format(_decimal(value), "f")
    except (InvalidOperation, TypeError, ValueError):
        return "0"


def _require_admin():
    if frappe.session.user == "Administrator":
        return
    frappe.only_for("System Manager")


def _meta_error_status(error):
    message = str(error)
    match = re.search(r"HTTP (\d{3})", message)
    if match:
        return f"Meta API request failed (HTTP {match.group(1)})."
    if "pagination" in message.casefold() or "page limit" in message.casefold():
        return "Meta returned an incomplete paginated response."
    return "Meta spend could not be read. Check token access and account permissions."
