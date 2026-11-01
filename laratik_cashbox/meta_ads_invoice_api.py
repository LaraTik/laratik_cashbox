"""The customer invoice workspace, and the safe creation of a draft invoice.

─── WHAT THIS MODULE IS ─────────────────────────────────────────────────────────

The brief asks for a customer billing management view:

    "For each bill-to customer, show: reporting/billing period, source Meta
     accounts and advertisers, rechargeable advertising cost, calculated
     service fees, billing currency, exchange rate status, tax configuration,
     amounts previously invoiced, current invoice readiness, ERPNext Sales
     Invoice reference and status."

and for an invoice workflow that ends in an ERPNext draft:

    "Review charges → Validate → Preview → Create ERPNext Draft → Review/
     Submit in ERPNext → Track outstanding amount and customer payment status."

So there are three doors here, and they are not equal:

  1. `get_invoice_workspace` — a READ. What would be charged, whether it may
     be turned into a draft, and what was already charged.
  2. `preview_invoice` — a READ. Exactly what the draft would carry, line by
     line, with every source reference behind it.
  3. `create_invoice_draft` — the ONLY write, and it is deliberate. It
     creates an ERPNext Sales Invoice in DRAFT. It never submits it, never
     records a payment, and never posts an entry.

─── WHY THE MONEY IS NOT COMPUTED HERE ─────────────────────────────────────────

The figures in a billing group are computed by `get_spend_report`, and this
module reads THAT. It does not recompute the fee, the conversion or the
total, because two implementations of an invoice total is how a screen and a
ledger start to disagree — and that disagreement is found by a customer, not
by us.

What this module DOES compute is the CHARGE: what the invoice carries, given
the profile's charge model. That is a policy question with a policy answer,
and the answer is the profile's, not the app's.

─── WHY THE POLICY IS NEVER GUESSED ─────────────────────────────────────────────

The brief is explicit:

    "Do not assume whether LaraTik acts as principal or agent for advertising
     expenditure. This must remain configurable and subject to approved
     accounting policy."

So the billing profile carries the policy as fields that are UNSET by
default — `charge_model`, `recognition_model`, `income_account`,
`invoice_item`, `tax_treatment`, `tax_template`, and the `fee_is_configured`
flag — and an unset field means "not decided". A period that is not decided
is not ready, and it says WHICH decision is missing.

The surrounding functionality — the workspace, the preview, the arithmetic,
the currency conversion — works perfectly well without a policy. Only the
ONE action that needs a policy waits, and it waits visibly.

─── WHY "BLOCKED" AND "NEEDS APPROVAL" ARE DIFFERENT ───────────────────────────

A **blocked** period cannot be invoiced because the DATA is not there: the
exchange rate is missing, the currency's precision is unknown, another
invoice already covers part of the period. No approval fixes those; the data
must change.

A **needs-approval** period is waiting on a DECISION: the fee was never
configured, the charge model was never chosen, the tax treatment was never
stated. A human settles those, and the period becomes ready.

─── WHY THE DRAFT IS NEVER SUBMITTED ───────────────────────────────────────────

    "Never silently submit invoices, record customer payments, or create
     accounting entries."

`insert()` on a Sales Invoice creates a document with `docstatus = 0` — a
draft. Submitting it is a separate, explicit act in ERPNext, by a person,
in ERPNext. Nothing in this module calls `submit()`, and nothing in this
module records a payment. The invoice's own lifecycle is ERPNext's to run.

─── WHY A PERIOD IS NEVER INVOICED TWICE ────────────────────────────────────────

Every invoice leaves behind a `LaraTik Meta Ads Invoice Batch`: the period,
the policy snapshots, the totals, the source hash, and the ERPNext Sales
Invoice it points at. Before a draft is created, the period is checked
against those batches:

  - a batch covering EXACTLY the period → the period was already invoiced;
  - a batch OVERLAPPING part of it → the period overlaps an invoice, which
    is a conflict rather than a duplicate, and is refused.

A re-sync that changes the underlying spend does not rewrite an approved
period: the batch keeps the figures it was created with, and the source
hash says whether the current data still agrees with them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import frappe
from frappe.utils import getdate, now_datetime

from laratik_cashbox import meta_ads_invoicing as invoicing
from laratik_cashbox import outstanding_policy
from laratik_cashbox.meta_ads_api import (
    _billable_ad_rows,
    _company_name,
    _currency_precision,
    _date_range,
    _decimal,
    _decimal_text,
    _period_accounts,
    _period_ad_spend_rows,
    _profile_policies,
    _quantize,
    _require_admin,
    _selling_rate,
    _text,
    get_spend_report,
)


#: How many billing groups one workspace may report.
MAX_WORKSPACE_ROWS = 500

#: How many invoice lines one preview may carry.
MAX_PREVIEW_LINES = 2_000

#: The source named on every invoice line's exchange rate.
EXCHANGE_RATE_SOURCE = "ERPNext Currency Exchange (selling rate)"

#: The time a Meta Ads invoice is stamped with. The DATE carries the period
#: the invoice bills; the time is the start of that day, so the invoice sorts
#: inside the period it bills rather than at the moment it was created.
POSTING_TIME = "00:00:00"

#: The source named on every invoice line's advertising cost.
SPEND_SOURCE = "Meta Insights spend"


@frappe.whitelist(methods=["GET"])
def get_invoice_workspace(company, since, until, account_name=None):
    """What would be invoiced, to whom, and whether it may be.

    One row per BILLING GROUP — a payer arrangement, a customer, a fee
    schedule and a billing currency. A customer whose fee changed mid-period
    gets one row per schedule, because blending two rates into one number
    would invent a fee nobody agreed to.

    This is a READ. It creates nothing, and it changes nothing.
    """
    _require_admin()
    company_name = _company_name(company)
    start, end = _date_range(since, until)

    report = get_spend_report(company_name, start.isoformat(), end.isoformat(), account_name)
    groups = report.get("billing_groups") or []

    policies = _profile_policies(company_name, {group.get("name") for group in groups})
    batches = _invoice_batches(company_name)

    rows = []
    cache = {}
    for group in groups[:MAX_WORKSPACE_ROWS]:
        rows.append(
            _workspace_row(
                group,
                policies.get(group.get("name"), {}),
                batches,
                start,
                end,
                company_name,
                cache,
            )
        )

    return {
        "company": company_name,
        "since": start.isoformat(),
        "until": end.isoformat(),
        "complete": bool(report.get("complete")),
        "billing_ready": bool(report.get("billing_ready")),
        "truncated": bool(report.get("truncated")),
        "rows": rows,
        "rows_truncated": len(groups) > MAX_WORKSPACE_ROWS,
        "readiness_states": list(invoicing.READINESS_STATES),
        "approval_reasons": list(invoicing.NEEDS_APPROVAL_REASONS),
        "blocked_reasons": list(invoicing.BLOCKED_REASONS),
    }


@frappe.whitelist(methods=["GET"])
def get_outstanding_receivables(company):
    """What the company's customers still owe, from ERPNext's OWN receivables.

    ─── WHY THIS IS ASKED OF ERPNext ────────────────────────────────────────────

    The brief asks for "Outstanding customer receivables, when available from
    ERPNext", and the "when available" is load-bearing. A receivable is not a
    number this application computes: it is the difference between what was
    invoiced and what has been paid, and both of those live in ERPNext's
    ledger. Deriving it here would mean reading invoices and payments and
    subtracting them — a second implementation of a figure ERPNext already
    keeps, which is the same mistake as a second invoice total.

    So this reads ERPNext's `outstanding_amount` and says so.

    ─── WHY TWO AMOUNTS, AND WHICH ONE IS THE HEADLINE ───────────────────────────

    A Sales Invoice carries its amount in ITS OWN currency and in the
    COMPANY's currency. They differ whenever the invoice is not in the
    company's currency, and the difference is a real money difference, not a
    rounding one. The headline is the COMPANY-currency total, because that is
    the currency the company reports in and the only one in which the invoices
    add up. The per-invoice-currency totals are reported beside it, so a
    reader can see what is owed in the currency it was billed in.

    ─── WHY ONLY SUBMITTED INVOICES ──────────────────────────────────────────────

    A draft invoice is not a receivable: nobody owes it yet, and it may still
    be edited. Only a submitted invoice with money outstanding is money owed.

    ─── WHY THERE IS NO `base_outstanding_amount` HERE ───────────────────────────

    There is no such column in ERPNext 16, and reading it made this query fail
    on a real site — a failure that no mocked test could see, because a mock
    answers whatever the fixture says.

    ERPNext keeps the outstanding in the **party account's currency**
    (`accounts/utils.py` writes `outstanding["outstanding_in_account_currency"]`),
    and `conversion_rate` is the **transaction → company** rate. Those are two
    different currencies, so the amount is converted by ADR-017's rule — the
    rule the `/position` screen already uses — and never by a second opinion
    written here.

    So this reads `Account.account_currency` for the invoice's receivable
    account (the ERPNext `Account` doctype has no field called `currency`),
    `Company.default_currency` for the company, and converts each invoice
    through `outstanding_policy.to_company_currency`.
    """

    company_name = _company_name(company)
    rows = frappe.get_all(
        "Sales Invoice",
        filters={
            "company": company_name,
            "docstatus": 1,
            "outstanding_amount": [">", 0],
        },
        fields=[
            "name", "customer", "currency", "debit_to", "outstanding_amount",
            "conversion_rate", "posting_date", "due_date",
        ],
        order_by="posting_date asc, name asc",
        limit_page_length=1_000,
    )

    company_currency = frappe.db.get_value(
        "Company", company_name, outstanding_policy.COMPANY_CURRENCY_FIELD
    ) or ""

    # ONE query for the receivable accounts' currencies — not one per invoice.
    account_names = sorted({row.debit_to for row in rows if row.debit_to})
    account_currencies = {}
    if account_names:
        for account in frappe.get_all(
            "Account",
            filters={"name": ["in", account_names]},
            fields=["name", outstanding_policy.ACCOUNT_CURRENCY_FIELD],
            limit_page_length=len(account_names),
        ):
            account_currencies[account.name] = getattr(
                account, outstanding_policy.ACCOUNT_CURRENCY_FIELD, ""
            ) or ""

    by_company_currency = {}
    by_account_currency = {}
    invoices = []
    unresolved = []
    # An invoice whose AMOUNT is unknown cannot be counted in ANY breakdown, and
    # one whose amount is known but whose party account currency is unknown
    # cannot be GROUPED. They are counted apart, because the two withhold
    # different figures.
    ungroupable = 0
    for row in rows:
        invoice_currency = row.currency or ""
        party_account_currency = account_currencies.get(row.debit_to, "")
        outstanding = outstanding_policy.read_decimal(row.outstanding_amount)
        # An invoice can be GROUPED — its amount is readable and its account
        # currency is known — even when it cannot be CONVERTED to the company
        # currency. Those are two different questions, and the breakdown by
        # account currency only asks the first one.
        groupable = outstanding is not None and bool(party_account_currency)
        conversion = outstanding_policy.to_company_currency(
            row.outstanding_amount,
            party_account_currency,
            invoice_currency,
            company_currency,
            row.conversion_rate,
        )
        if not conversion.ok:
            # An invoice whose amount cannot be stated is NOT counted as zero.
            # It is left out of the totals, and it is named.
            unresolved.append({
                "name": row.name,
                "customer": row.customer or "",
                "currency": invoice_currency,
                "reason": conversion.reason or "",
                "detail": conversion.detail,
            })
            if not groupable:
                ungroupable += 1
            else:
                # The amount IS readable and IS in a known currency, so it
                # belongs in the breakdown by account currency — where it can
                # still be stated.
                account_currency = party_account_currency
                by_account_currency[account_currency] = (
                    by_account_currency.get(account_currency, Decimal(0)) + outstanding
                )
            continue
        # The figure ERPNext keeps is in the PARTY ACCOUNT's currency, and that
        # is the currency it is grouped under. Calling it the invoice currency
        # would be a label on a number that means something else.
        account_currency = party_account_currency
        by_account_currency[account_currency] = (
            by_account_currency.get(account_currency, Decimal(0)) + outstanding
        )
        by_company_currency[company_currency] = (
            by_company_currency.get(company_currency, Decimal(0)) + conversion.amount
        )
        invoices.append({
            "name": row.name,
            "customer": row.customer or "",
            "currency": invoice_currency,
            "party_account_currency": party_account_currency,
            "outstanding_amount": format(outstanding, "f"),
            "company_currency_amount": format(conversion.amount, "f"),
            "conversion_basis": conversion.basis or "",
            "posting_date": str(row.posting_date or ""),
            "due_date": str(row.due_date or ""),
        })

    # ─── WHY A PARTIAL TOTAL IS WITHHELD ──────────────────────────────────────
    #
    # The company-currency total is the sum of what every invoice contributes.
    # If one invoice's amount cannot be converted, that total is SMALLER than
    # the truth, and nothing on the Overview's tile says so — a reader sees a
    # number that looks complete and is not. A partial total is the dangerous
    # shape: it understates what the customers owe, quietly.
    #
    # So the total is stated only when EVERY invoice could be stated. When one
    # could not, the figure is withheld, the count is reported, and the
    # invoices are NAMED — so the reader can go and fix them.
    #
    # The account-currency breakdown is a different figure with a different
    # condition: it holds what ERPNext keeps, so it needs the amount and the
    # party account currency, but it does NOT need a company conversion. An
    # invoice that is missing a conversion rate is therefore excluded from the
    # COMPANY total and still counted in the ACCOUNT breakdown.
    total_complete = not unresolved
    account_breakdown_complete = ungroupable == 0

    if not total_complete:
        total_reason = (
            "{0} invoice{1} could not be stated in the company currency ({2}), so "
            "the total is withheld rather than shown as a partial figure.".format(
                len(unresolved),
                "" if len(unresolved) == 1 else "s",
                company_currency or "the company's currency",
            )
        )
    else:
        total_reason = ""

    if not account_breakdown_complete:
        account_breakdown_reason = (
            "{0} invoice{1} report no readable outstanding amount, or no party "
            "account currency, so the breakdown by account currency is withheld "
            "rather than shown as a partial figure.".format(
                ungroupable,
                "" if ungroupable == 1 else "s",
            )
        )
    else:
        account_breakdown_reason = ""

    return {
        "company": company_name,
        "available": True,
        "company_currency": company_currency,
        # The COMPANY-currency total. Empty when it is withheld — never a
        # partial sum.
        "total": (
            {currency: format(amount, "f") for currency, amount in sorted(by_company_currency.items())}
            if total_complete
            else {}
        ),
        "total_complete": total_complete,
        "total_reason": total_reason,
        # The currency ERPNext actually keeps the outstanding in: the party
        # account's currency.
        "by_account_currency": (
            {currency: format(amount, "f") for currency, amount in sorted(by_account_currency.items())}
            if account_breakdown_complete
            else {}
        ),
        "by_account_currency_complete": account_breakdown_complete,
        "by_account_currency_reason": account_breakdown_reason,
        "invoice_count": len(invoices),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "invoices": invoices,
        "note": (
            "Outstanding amounts are ERPNext's own receivables, not a figure this "
            "application computes. They are kept by ERPNext in the party account's "
            "currency and converted to the company currency by ADR-017's rule. "
            "A total that could not be stated for every invoice is withheld, not "
            "shown as a partial figure."
        ),
    }


@frappe.whitelist(methods=["GET"])
def preview_invoice(company, since, until, billing_profile, billing_customer, fee_percent, invoice_currency):
    """Exactly what the draft would carry, line by line.

    A READ. It computes nothing that is stored and changes nothing.
    """
    _require_admin()
    company_name = _company_name(company)
    start, end = _date_range(since, until)
    key = _group_key(billing_profile, billing_customer, fee_percent, invoice_currency)

    prepared = _prepare(company_name, start, end, key)
    return prepared["preview"]


@frappe.whitelist(methods=["POST"])
def create_invoice_draft(company, since, until, billing_profile, billing_customer, fee_percent, invoice_currency):
    """Create the ERPNext Sales Invoice DRAFT for one billing group.

    This is the only write in the invoice workflow, and it is deliberate:

      - the figures are RECOMPUTED here, never taken from the client, so a
        stale or edited preview cannot become an invoice;
      - the readiness is RECOMPUTED here, so the gate cannot be bypassed by
        asking nicely;
      - the ERPNext Sales Invoice is created as a DRAFT. It is never
        submitted, and no payment is recorded;
      - the period is checked against the batches that already exist, so the
        same period is never invoiced twice.
    """
    _require_admin()
    company_name = _company_name(company)
    start, end = _date_range(since, until)
    key = _group_key(billing_profile, billing_customer, fee_percent, invoice_currency)

    prepared = _prepare(company_name, start, end, key)
    preview = prepared["preview"]

    state = preview["readiness"]
    if state != invoicing.READINESS_READY:
        frappe.throw(
            "This period is not ready to invoice ({0}). {1}".format(
                state,
                "; ".join(preview["readiness_reasons"]) or "No reason was recorded.",
            )
        )

    batch = _create_batch(company_name, preview, prepared)
    return {
        "batch": batch,
        "sales_invoice": preview["sales_invoice_created"],
        "status": batch["status"],
        "message": "The ERPNext Sales Invoice was created as a DRAFT. It has not been submitted.",
    }


# ─── THE SITE FACTS THE READINESS NEEDS ────────────────────────────────────────
#
# ─── WHY THESE ARE READ HERE, AND NOT LEFT TO ERPNext ─────────────────────────
#
# The draft is created by ERPNext, and ERPNext will refuse a draft that does
# not fit its own rules. Two of those refusals are about facts the app can
# read BEFORE it writes:
#
#   * the RECEIVABLE ACCOUNT the customer's invoice is booked against must be
#     in the invoice's currency — "Party Account ... currency ... and document
#     currency ... should be same";
#   * the invoice must be TRANSLATABLE into the company's books, which needs
#     an approved rate for the pair.
#
# So the app resolves both itself, and a period that ERPNext would refuse is
# refused here, with a reason the screen can name and an operator can act on.
# The alternative — offering a "Create draft" button that fails with a raw
# ERPNext error — is a worse product and the same fact.


def _company_currency(company_name):
    """The currency the company's books are kept in."""
    return frappe.db.get_value("Company", company_name, "default_currency") or ""


def _receivable_account(company_name, customer):
    """The receivable account this customer is billed through, and its currency.

    ─── WHY THIS MIRRORS ERPNext, AND WHY IT HAS TO ───────────────────────────

    ERPNext resolves a customer's receivable account in this order, and the
    invoice it creates MUST carry the account it resolved:

        1. the CUSTOMER's `Party Account` row for this company;
        2. the CUSTOMER GROUP's `Party Account` row for this company;
        3. the COMPANY's default receivable account.

    So the app resolves the same account, in the same order, and checks it
    itself. The invoice then NAMES that account, so the draft is pinned to
    the receivable it was checked against.
    """
    account = frappe.db.get_value(
        "Party Account",
        {"parenttype": "Customer", "parent": customer, "company": company_name},
        "account",
    )
    if not account:
        group = frappe.db.get_value("Customer", customer, "customer_group")
        if group:
            account = frappe.db.get_value(
                "Party Account",
                {"parenttype": "Customer Group", "parent": group, "company": company_name},
                "account",
            )
    if not account:
        account = frappe.db.get_value("Company", company_name, "default_receivable_account")
    currency = ""
    if account:
        currency = frappe.db.get_value("Account", account, "account_currency") or ""
    return account or "", currency


def _party_ledger_currency(company_name, customer):
    """The currency the customer's POSTED ledger entries are in, if any.

    ERPNext skips its party-account currency check when the party already has
    a posted entry — the customer is then known to carry more than one
    currency, and one receivable account serves both.
    """
    rows = frappe.get_all(
        "GL Entry",
        filters={
            "party_type": "Customer",
            "party": customer,
            "company": company_name,
            "docstatus": 1,
            "is_cancelled": 0,
        },
        fields=["account_currency"],
        limit_page_length=1,
    )
    for row in rows:
        currency = getattr(row, "account_currency", None) or ""
        if currency:
            return currency
    return ""


def _multi_currency_party_accounts_allowed():
    """Whether ERPNext's own setting lets one party account carry many currencies."""
    return bool(
        frappe.db.get_single_value(
            "Accounts Settings",
            "allow_multi_currency_invoices_against_single_party_account",
        )
    )


def _receivable_account_status(company_name, customer, invoice_currency):
    """Whether this customer may be invoiced in this currency — and why not.

    The two escape hatches are ERPNext's own, and they are checked in
    ERPNext's order: a party that already has a posted ledger entry, and the
    site setting that allows one party account to carry many currencies.
    """
    account, account_currency = _receivable_account(company_name, customer)
    if not account:
        return False, invoicing.BLOCKED_REASON_RECEIVABLE_ACCOUNT_MISSING
    if not invoice_currency or account_currency == invoice_currency:
        return True, ""
    if _multi_currency_party_accounts_allowed():
        return True, ""
    if _party_ledger_currency(company_name, customer):
        return True, ""
    return False, invoicing.BLOCKED_REASON_RECEIVABLE_ACCOUNT_CURRENCY_MISMATCH


def _stale_rates_allowed():
    """Whether the site accepts an exchange rate older than its staleness window."""
    return bool(frappe.db.get_single_value("Accounts Settings", "allow_stale"))


def _stale_days():
    """The site's staleness window, in days."""
    try:
        return int(frappe.db.get_single_value("Accounts Settings", "stale_days") or 0)
    except (TypeError, ValueError):
        return 0


def _company_conversion_rate(invoice_currency, company_currency, on_date):
    """The rate the INVOICE carries: its own currency into the company's books.

    ─── WHY THIS IS NOT THE LINE'S RATE ─────────────────────────────────────

    A LINE's rate converts the advertising cost into the billing currency
    (basis → invoice). THIS one converts the invoice into the company's books
    (invoice → company). They are different pairs, they point opposite ways,
    and ERPNext's field is the second one: `conversion_rate`.

    ─── WHY IT IS THE SITE'S OWN RATE ───────────────────────────────────────

    It is read from the site's `Currency Exchange` — the rates an accountant
    approved — and from nowhere else. It is never derived, never inverted,
    never approximated and never fetched from a third party. A site with no
    approved rate for the pair gets NO rate, and the period is BLOCKED: an
    invoice that cannot be translated is not an invoice at a rate of one.

    The staleness rule is ERPNext's own (`Accounts Settings.allow_stale` and
    `stale_days`), so the app and ERPNext agree on which rates count.
    """
    if not invoice_currency or not company_currency:
        return None, None
    if invoice_currency == company_currency:
        return Decimal(1), on_date.isoformat()
    rows = frappe.get_all(
        "Currency Exchange",
        filters={
            "from_currency": invoice_currency,
            "to_currency": company_currency,
            "for_selling": 1,
            "date": ["<=", on_date.isoformat()],
        },
        fields=["date", "exchange_rate"],
        order_by="date desc",
        limit_page_length=1,
    )
    if not rows:
        return None, None
    row = rows[0]
    rate_date = getdate(row.date)
    if not _stale_rates_allowed():
        checkpoint = on_date - timedelta(days=_stale_days())
        if rate_date <= checkpoint:
            return None, None
    try:
        rate = _decimal(row.exchange_rate)
    except (InvalidOperation, TypeError, ValueError):
        return None, None
    if rate <= 0:
        return None, None
    return rate, rate_date.isoformat()


# ─── THE READINESS RULE ────────────────────────────────────────────────────────


def _readiness_reasons(
    company_name,
    group,
    policy,
    end,
    missing_rate,
    precision_known,
    covering,
    cache=None,
):
    """The reasons a period is not ready — ONE rule, for both readers.

    ─── WHY THERE IS ONE RULE AND NOT TWO ───────────────────────────────────

    The workspace row and the preview used to work the reasons out separately,
    and two copies of one rule can disagree: a period the workspace called
    READY could be refused by the preview, and a reader would have had no way
    of knowing which was right. So the reasons are worked out ONCE, here, and
    both readers ask this function.

    `cache` carries the site facts across the rows of one workspace, so a
    workspace of two hundred rows reads each customer's receivable ONCE.
    """
    cache = cache if cache is not None else {}
    approval_reasons = []
    blocked_reasons = []

    if group.get("chargeability") != "assigned_billable":
        blocked_reasons.append(invoicing.BLOCKED_REASON_SPEND_NOT_ATTRIBUTABLE)
    if missing_rate:
        blocked_reasons.append(invoicing.BLOCKED_REASON_EXCHANGE_RATE_MISSING)
    if not precision_known:
        blocked_reasons.append(invoicing.BLOCKED_REASON_CURRENCY_PRECISION_UNKNOWN)
    if any(not batch["exact"] for batch in covering):
        blocked_reasons.append(invoicing.BLOCKED_REASON_PERIOD_OVERLAPS_AN_INVOICE)

    # The RECEIVABLE the invoice would be booked against.
    customer = group.get("customer") or ""
    invoice_currency = group.get("invoice_currency") or ""
    receivable_key = (company_name, customer, invoice_currency)
    if receivable_key not in cache:
        cache[receivable_key] = _receivable_account_status(
            company_name, customer, invoice_currency
        )
    receivable_ok, receivable_reason = cache[receivable_key]
    if not receivable_ok:
        blocked_reasons.append(receivable_reason)

    # The RATE the invoice would carry into the company's books.
    company_key = ("company_currency", company_name)
    if company_key not in cache:
        cache[company_key] = _company_currency(company_name)
    rate_key = ("conversion_rate", company_name, invoice_currency, end.isoformat())
    if rate_key not in cache:
        cache[rate_key] = _company_conversion_rate(
            invoice_currency, cache[company_key], end
        )
    conversion_rate, _conversion_rate_date = cache[rate_key]
    if conversion_rate is None:
        blocked_reasons.append(invoicing.BLOCKED_REASON_EXCHANGE_RATE_MISSING)

    # The DECISIONS. These are not data gaps: a human settles them.
    if (group.get("fee_status") or invoicing.FEE_STATUS_UNCONFIGURED) == invoicing.FEE_STATUS_UNCONFIGURED:
        approval_reasons.append(invoicing.NEEDS_APPROVAL_REASON_FEE_UNCONFIGURED)
    if (policy.get("charge_model") or "") not in invoicing.CHARGE_MODELS:
        approval_reasons.append(invoicing.NEEDS_APPROVAL_REASON_CHARGE_MODEL_NOT_DECIDED)
    if (policy.get("recognition_model") or "") not in invoicing.RECOGNITION_MODELS:
        approval_reasons.append(invoicing.NEEDS_APPROVAL_REASON_RECOGNITION_MODEL_NOT_DECIDED)
    if not policy.get("income_account"):
        approval_reasons.append(invoicing.NEEDS_APPROVAL_REASON_INCOME_ACCOUNT_MISSING)
    if not policy.get("invoice_item"):
        approval_reasons.append(invoicing.NEEDS_APPROVAL_REASON_INVOICE_ITEM_MISSING)
    if (policy.get("tax_treatment") or "") not in invoicing.TAX_TREATMENTS:
        approval_reasons.append(invoicing.NEEDS_APPROVAL_REASON_TAX_TREATMENT_NOT_DECIDED)
    elif policy.get("tax_treatment") == invoicing.TAX_TREATMENT_TEMPLATE and not policy.get("tax_template"):
        approval_reasons.append(invoicing.NEEDS_APPROVAL_REASON_TAX_TEMPLATE_MISSING)

    return approval_reasons, _unique(blocked_reasons)


def _unique(reasons):
    """The reasons, in order, without the same reason twice."""
    seen = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return seen


# ─── THE WORKSPACE ROW ─────────────────────────────────────────────────────────


def _workspace_row(group, policy, batches, start, end, company_name="", cache=None):
    """One customer row: the charge, its readiness, and what was charged before."""
    covering = _covering_batches(batches, group, start, end)
    charge = _charge_of(group, policy, end, covering)

    missing_rate = any(
        entry.get("rate") in (None, "") for entry in (group.get("fx") or [])
    )
    precision_known = bool(_currency_precision(group.get("invoice_currency") or ""))

    approval_reasons, blocked_reasons = _readiness_reasons(
        company_name,
        group,
        policy,
        end,
        missing_rate,
        precision_known,
        covering,
        cache,
    )

    already_invoiced = any(batch["exact"] for batch in covering)
    state, reasons = invoicing.classify_readiness({
        "already_invoiced": already_invoiced,
        "chargeable_spend": charge["chargeable_spend"],
        "blocked_reasons": blocked_reasons,
        "approval_reasons": approval_reasons,
    })

    return {
        "profile": group.get("name", ""),
        "customer": group.get("customer", ""),
        "customer_name": group.get("customer_name", ""),
        "relationship_kind": group.get("relationship_kind", ""),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "accounts": [
            {"name": name, "account_name": label}
            for name, label in sorted((group.get("accounts") or {}).items())
        ],
        "page_assets": group.get("page_assets") or [],
        "rechargeable_cost": group.get("source_spend") or {},
        "service_fee": group.get("service_fee") or {},
        "fee_percent": group.get("fee_percent", ""),
        "fee_status": group.get("fee_status", invoicing.FEE_STATUS_UNCONFIGURED),
        "invoice_currency": group.get("invoice_currency", ""),
        "charge_model": policy.get("charge_model", ""),
        "recognition_model": policy.get("recognition_model", ""),
        "income_account": policy.get("income_account", ""),
        "invoice_item": policy.get("invoice_item", ""),
        "tax_treatment": policy.get("tax_treatment", ""),
        "tax_template": policy.get("tax_template", ""),
        "fx": group.get("fx") or [],
        "fx_status": charge["fx_status"],
        "invoice_amount": charge["invoice_amount"],
        "invoice_amount_reason": group.get("invoice_amount_reason", ""),
        "previously_invoiced": charge["previously_invoiced"],
        "invoiced_periods": [
            {
                "batch": batch["batch"],
                "period_start": batch["period_start"],
                "period_end": batch["period_end"],
                "total": batch["total"],
                "sales_invoice": batch["sales_invoice"],
                "status": batch["status"],
                "exact": batch["exact"],
            }
            for batch in covering
        ],
        "readiness": state,
        "readiness_reasons": reasons,
        "sales_invoice": covering[0]["sales_invoice"] if covering and covering[0]["exact"] else "",
        "sales_invoice_status": covering[0]["status"] if covering and covering[0]["exact"] else "",
    }


def _charge_of(group, policy, end, covering):
    """What the invoice would carry, and what was already charged.

    `covering` is the batches that touch this period. Their totals are what
    was invoiced before, in the currency each batch was billed in — and they
    are reported in their OWN currencies, because a batch's currency is the
    currency its customer was billed in.
    """
    charge_model = policy.get("charge_model") or ""
    invoice_currency = group.get("invoice_currency") or ""
    basis = group.get("source_spend") or {}
    fees = group.get("service_fee") or {}

    decided = charge_model in invoicing.CHARGE_MODELS
    precision = _currency_precision(invoice_currency) if invoice_currency else None
    precision_known = precision is not None

    # The charge is converted into the INVOICE currency, at the rate the
    # report found — never at a rate of one, and never at a rate this module
    # chose. A missing rate makes the total unavailable, not zero.
    rates = {}
    for entry in group.get("fx") or []:
        rate_text = entry.get("rate")
        if rate_text in (None, ""):
            continue
        try:
            rates[entry.get("from")] = Decimal(str(rate_text))
        except (InvalidOperation, TypeError, ValueError):
            continue

    total = Decimal(0)
    converted = True
    if decided and precision_known:
        for currency, amount_text in sorted(basis.items()):
            try:
                amount = Decimal(str(amount_text))
                fee = Decimal(str(fees.get(currency, "0")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if charge_model == invoicing.CHARGE_MODEL_RECHARGE:
                charge_in_source = amount + fee
            else:
                charge_in_source = fee
            rate = rates.get(currency)
            if rate is None:
                # A missing rate makes the amount UNAVAILABLE. It does not
                # make it zero: a zero total is a bill, and a bill of
                # nothing is the one answer this application must never give.
                converted = False
                continue
            total += charge_in_source * rate

    chargeable = Decimal(0)
    for amount_text in basis.values():
        try:
            chargeable += Decimal(str(amount_text))
        except (InvalidOperation, TypeError, ValueError):
            continue

    fx_status = "complete"
    if not invoice_currency:
        fx_status = "billing_currency_missing"
    else:
        for entry in group.get("fx") or []:
            if entry.get("rate") in (None, ""):
                fx_status = "exchange_rate_missing"
                break
    if fx_status == "complete" and not precision_known:
        fx_status = "currency_precision_unknown"

    previously_invoiced = {}
    for batch in covering:
        currency = batch.get("invoice_currency") or ""
        if not currency:
            continue
        try:
            amount = Decimal(batch.get("total") or "0")
        except (InvalidOperation, TypeError, ValueError):
            continue
        previously_invoiced[currency] = previously_invoiced.get(currency, Decimal(0)) + amount

    return {
        "charge_model": charge_model,
        "invoice_amount": (
            format(_quantize(total, precision), "f")
            if decided and precision_known and converted
            else None
        ),
        "chargeable_spend": chargeable if chargeable > 0 else Decimal(0),
        "precision_known": precision_known,
        "fx_status": fx_status,
        "previously_invoiced": {
            currency: format(amount, "f") for currency, amount in sorted(previously_invoiced.items())
        },
    }


# ─── THE PREVIEW ───────────────────────────────────────────────────────────────

def _prepare(company_name, start, end, group_key):
    """Everything the preview and the draft need, computed ONCE, on the server.

    The billable spend is selected by the SAME function the spend report
    uses, so the invoice cannot disagree with the report about which spend
    is billable.
    """
    report = get_spend_report(company_name, start.isoformat(), end.isoformat(), None)
    groups = report.get("billing_groups") or []
    group = next((candidate for candidate in groups if candidate.get("key") == group_key), None)
    if group is None:
        frappe.throw("That billing arrangement has no chargeable spend for this period.")

    policies = _profile_policies(company_name, {group.get("name")})
    policy = policies.get(group.get("name"), {})

    billable_rows = _billable_spend_rows(company_name, start, end, group)

    lines = _invoice_lines(billable_rows, group, policy, end)
    charge_model = policy.get("charge_model") or ""
    invoice_currency = group.get("invoice_currency") or ""
    precision = _currency_precision(invoice_currency) if invoice_currency else None

    basis_total = Decimal(0)
    fee_total = Decimal(0)
    charge_total = Decimal(0)
    for line in lines:
        basis_total += Decimal(line["basis_amount"])
        fee_total += Decimal(line["fee_amount"])
        charge_total += Decimal(line["charge_amount"])

    state, reasons = _readiness(company_name, group, policy, lines, start, end, chargeable=basis_total)

    preview = {
        "company": company_name,
        "profile": group.get("name", ""),
        "customer": group.get("customer", ""),
        "customer_name": group.get("customer_name", ""),
        "relationship_kind": group.get("relationship_kind", ""),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "invoice_currency": invoice_currency,
        "charge_model": charge_model,
        "recognition_model": policy.get("recognition_model", ""),
        "income_account": policy.get("income_account", ""),
        "invoice_item": policy.get("invoice_item", ""),
        "tax_treatment": policy.get("tax_treatment", ""),
        "tax_template": policy.get("tax_template", ""),
        "fee_percent": group.get("fee_percent", ""),
        "fee_status": group.get("fee_status", invoicing.FEE_STATUS_UNCONFIGURED),
        "page_assets": group.get("page_assets") or [],
        "lines": lines,
        "totals": {
            "basis": format(basis_total, "f"),
            "fee": format(fee_total, "f"),
            "charge": format(charge_total, "f"),
        },
        "readiness": state,
        "readiness_reasons": reasons,
        "source_hash": _source_hash(group, lines, policy),
        "sales_invoice_created": "",
    }
    return {"preview": preview, "group": group, "policy": policy, "lines": lines}


def _billable_spend_rows(company_name, start, end, group):
    """The billable ad rows of one group, through the shared selection.

    The selection is the one the spend report uses — the same accounts, the
    same completed runs, the same "may this row be charged" rule — so the
    invoice and the report can never hold different views of the same spend.
    """
    period = _period_accounts(company_name, start, end, None)
    account_by_name = {account["name"]: account for account in period["accounts"]}
    complete_by_account = period["complete_by_account"]
    run_by_account = {
        account["name"]: complete_by_account.get(account["external_account_id"])
        for account in period["accounts"]
    }
    ad_rows = _period_ad_spend_rows(
        company_name, start, end, period["selected_run_names"], None
    )
    billable, _unreadable = _billable_ad_rows(ad_rows, account_by_name, run_by_account)
    return [
        row for row in billable
        if row["profile"] == group.get("name")
        and row["customer"] == group.get("customer")
        and format(row["fee_percent"], "f") == group.get("fee_percent")
        and row["invoice_currency"] == group.get("invoice_currency")
    ]


def _invoice_lines(billable_rows, group, policy, end):
    """The lines a draft would carry: one per ad account and source currency."""
    by_line = {}
    for row in billable_rows:
        key = (row["account"], row["currency"])
        entry = by_line.setdefault(key, {
            "account": row["account"],
            "account_name": row["account_name"],
            "external_account_id": row["external_account_id"],
            "basis_currency": row["currency"],
            "basis_amount": Decimal(0),
            "fee_percent": row["fee_percent"],
            "fee_amount": Decimal(0),
            "source_sync": row["sync_run"],
            "ad_ids": [],
            "page_assets": [],
        })
        entry["basis_amount"] += row["amount"]
        entry["fee_amount"] += invoicing.fee_amount(row["amount"], row["fee_percent"])
        if row["ad_id"] not in entry["ad_ids"]:
            entry["ad_ids"].append(row["ad_id"])
        if row["page_asset"] not in entry["page_assets"]:
            entry["page_assets"].append(row["page_asset"])

    invoice_currency = group.get("invoice_currency") or ""
    lines = []
    for key in sorted(by_line):
        entry = by_line[key]
        basis = _quantize(entry["basis_amount"], _precision_of(entry["basis_currency"]))
        fee = _quantize(entry["fee_amount"], _precision_of(entry["basis_currency"]))
        rate, rate_date = _selling_rate(entry["basis_currency"], invoice_currency, end)
        if rate is None:
            converted_basis = None
            converted_fee = None
            charge = None
        else:
            converted_basis = _quantize(basis * rate, _precision_of(invoice_currency))
            converted_fee = _quantize(fee * rate, _precision_of(invoice_currency))
            charge = _quantize(converted_basis + converted_fee, _precision_of(invoice_currency))
        lines.append({
            "account": entry["account"],
            "account_name": entry["account_name"],
            "external_account_id": entry["external_account_id"],
            "basis_currency": entry["basis_currency"],
            "basis_amount": format(basis, "f"),
            "fee_percent": format(entry["fee_percent"], "f"),
            "fee_amount": format(fee, "f"),
            "invoice_currency": invoice_currency,
            "exchange_rate": format(rate, "f") if rate is not None else None,
            "exchange_rate_date": rate_date,
            "exchange_rate_source": EXCHANGE_RATE_SOURCE if rate is not None else "",
            "converted_basis": format(converted_basis, "f") if converted_basis is not None else None,
            "converted_fee": format(converted_fee, "f") if converted_fee is not None else None,
            "charge_amount": format(charge, "f") if charge is not None else None,
            "source_sync": entry["source_sync"],
            "ad_ids": entry["ad_ids"],
            "page_assets": entry["page_assets"],
        })
    return lines[:MAX_PREVIEW_LINES]


def _precision_of(currency):
    precision = _currency_precision(currency) if currency else None
    return precision if precision is not None else 2


def _readiness(company_name, group, policy, lines, start, end, chargeable):
    """The readiness of one period, and the reasons for it.

    ─── WHY IT ASKS THE SHARED RULE ─────────────────────────────────────────

    The workspace row and this function used to work the reasons out
    separately, and two copies of one rule can disagree: the workspace could
    call a period READY and this function could refuse it, and a reader would
    have had no way of knowing which was right. So both ask `_readiness_reasons`.
    """
    covering = _covering_batches(_invoice_batches(company_name), group, start, end)
    missing_rate = any(line.get("exchange_rate") in (None, "") for line in lines)
    precision_known = bool(_currency_precision(group.get("invoice_currency") or ""))

    approval_reasons, blocked_reasons = _readiness_reasons(
        company_name,
        group,
        policy,
        end,
        missing_rate,
        precision_known,
        covering,
    )

    return invoicing.classify_readiness({
        "already_invoiced": any(batch["exact"] for batch in covering),
        "chargeable_spend": chargeable,
        "blocked_reasons": blocked_reasons,
        "approval_reasons": approval_reasons,
    })


# ─── THE BATCHES ───────────────────────────────────────────────────────────────

def _invoice_batches(company_name):
    """Every invoice batch of a company, with its ERPNext status read back.

    The status is read from ERPNext rather than trusted from our own record,
    because a person may submit or cancel the draft in ERPNext and the
    workspace has to show what actually happened.
    """
    if not company_name:
        return []
    rows = frappe.get_all(
        "LaraTik Meta Ads Invoice Batch",
        filters={"company": company_name},
        fields=[
            "name", "billing_profile", "billing_customer", "period_start", "period_end",
            "invoice_currency", "status", "sales_invoice", "total", "basis_total",
            "fee_total", "source_hash", "status_synced_at",
        ],
        order_by="created_at desc",
        limit_page_length=1_000,
    )
    batches = []
    for row in rows:
        status = row.status or "Draft"
        sales_invoice = row.sales_invoice or ""
        if sales_invoice:
            live = _sales_invoice_status(sales_invoice)
            if live and live != status:
                status = live
                _record_status(row.name, status)
        batches.append({
            "batch": row.name,
            "profile": row.billing_profile or "",
            "customer": row.billing_customer or "",
            "period_start": str(row.period_start or ""),
            "period_end": str(row.period_end or ""),
            "invoice_currency": row.invoice_currency or "",
            "status": status,
            "sales_invoice": sales_invoice,
            "total": _decimal_text(row.total or "0"),
            "basis_total": _decimal_text(row.basis_total or "0"),
            "fee_total": _decimal_text(row.fee_total or "0"),
            "source_hash": row.source_hash or "",
        })
    return batches


def _covering_batches(batches, group, start, end):
    """The batches that touch this period, and whether each covers it exactly."""
    covering = []
    for batch in batches:
        if batch["profile"] != group.get("name"):
            continue
        if batch["customer"] != group.get("customer"):
            continue
        try:
            batch_start = getdate(batch["period_start"])
            batch_end = getdate(batch["period_end"])
        except (TypeError, ValueError):
            continue
        if batch_end < start or batch_start > end:
            continue
        covering.append({
            **batch,
            "exact": batch_start == start and batch_end == end,
        })
    return covering


def _sales_invoice_status(sales_invoice):
    """ERPNext's own view of the draft: 0 Draft, 1 Submitted, 2 Cancelled."""
    docstatus = frappe.db.get_value("Sales Invoice", sales_invoice, "docstatus")
    if docstatus is None:
        return ""
    return {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(int(docstatus), "")


def _record_status(batch_name, status):
    """Remember what ERPNext says, so the next read is a comparison, not a guess."""
    frappe.db.set_value(
        "LaraTik Meta Ads Invoice Batch",
        batch_name,
        {"status": status, "status_synced_at": now_datetime()},
        update_modified=False,
    )


# ─── THE WRITE ─────────────────────────────────────────────────────────────────

def _create_batch(company_name, preview, prepared):
    """Create the ERPNext draft, then the batch and its lines that point at it.

    The ORDER matters. The Sales Invoice is created FIRST, because the batch
    records its reference — and a batch that points at an invoice that does
    not exist is worse than no batch at all.
    """
    policy = prepared["policy"]
    group = prepared["group"]
    lines = prepared["lines"]

    invoice_currency = preview["invoice_currency"]
    total = Decimal(preview["totals"]["charge"])

    # The RECEIVABLE this invoice is booked against, and the RATE it carries
    # into the company's books. The readiness already checked both, and they
    # are read again here because a draft is a DOCUMENT: it has to name the
    # account it was checked against, and carry the rate that was checked.
    receivable_account, _account_currency = _receivable_account(
        company_name, preview["customer"]
    )
    conversion_rate, _conversion_rate_date = _company_conversion_rate(
        invoice_currency,
        _company_currency(company_name),
        getdate(preview["period_end"]),
    )

    sales_invoice = frappe.get_doc({
        "doctype": "Sales Invoice",
        "company": company_name,
        "customer": preview["customer"],
        "currency": invoice_currency,
        "debit_to": receivable_account or None,
        # ─── WHY THE POSTING DATE IS SET THIS WAY ──────────────────────────
        # ERPNext's `TransactionBase.validate_posting_time` OVERWRITES the
        # posting date with TODAY unless `set_posting_time` is set. So an
        # invoice for a period that ended last month would be posted in this
        # month — in the wrong accounting period, and silently, because the
        # date the caller asked for simply disappeared.
        #
        # The flag is ERPNext's own mechanism for a document that carries its
        # own date — the one a back-dated invoice is created with in the Desk.
        # The date is therefore the period this invoice bills, and the revenue
        # lands in the period the advertising was bought in.
        "posting_date": preview["period_end"],
        "posting_time": POSTING_TIME,
        "set_posting_time": 1,
        "items": [_sales_invoice_item(line, policy) for line in lines],
        "taxes_and_charges": _tax_template(policy),
        "remarks": "LaraTik Meta Ads — {0} to {1} — source hash {2}".format(
            preview["period_start"], preview["period_end"], preview["source_hash"]
        ),
    })
    if conversion_rate is not None:
        # ERPNext's field is `conversion_rate` — the invoice's OWN currency
        # into the company's. This module once wrote `exchange_rate` here,
        # which is NOT a field on an ERPNext 16 Sales Invoice: the write was
        # silent, the invoice carried no rate at all, and the intent was
        # lost without a word.
        sales_invoice.conversion_rate = float(conversion_rate)
    # `insert()` creates a DRAFT. Nothing here submits it, and nothing here
    # records a payment: the invoice's lifecycle belongs to ERPNext.
    sales_invoice.insert()
    preview["sales_invoice_created"] = sales_invoice.name

    batch = frappe.new_doc("LaraTik Meta Ads Invoice Batch")
    batch.company = company_name
    batch.billing_profile = preview["profile"]
    batch.billing_customer = preview["customer"]
    batch.relationship_kind = preview["relationship_kind"]
    batch.period_start = preview["period_start"]
    batch.period_end = preview["period_end"]
    batch.invoice_currency = invoice_currency
    batch.charge_model_snapshot = preview["charge_model"]
    batch.recognition_model_snapshot = preview["recognition_model"]
    batch.fee_percent_snapshot = preview["fee_percent"]
    batch.tax_treatment_snapshot = preview["tax_treatment"]
    batch.tax_template_snapshot = policy.get("tax_template", "")
    batch.income_account_snapshot = policy.get("income_account", "")
    batch.invoice_item_snapshot = policy.get("invoice_item", "")
    batch.status = "Draft"
    batch.sales_invoice = sales_invoice.name
    batch.status_synced_at = now_datetime()
    batch.basis_total = preview["totals"]["basis"]
    batch.fee_total = preview["totals"]["fee"]
    batch.total = preview["totals"]["charge"]
    batch.source_sync = _source_sync(lines)
    batch.source_hash = preview["source_hash"]
    batch.created_at = now_datetime()
    batch.created_by = frappe.session.user
    batch.insert()

    for line in lines:
        invoice_line = frappe.new_doc("LaraTik Meta Ads Invoice Line")
        invoice_line.invoice_batch = batch.name
        invoice_line.company = company_name
        invoice_line.ad_account = line["account"]
        invoice_line.external_account_id = line["external_account_id"]
        invoice_line.basis_currency = line["basis_currency"]
        invoice_line.basis_amount = line["basis_amount"]
        invoice_line.fee_percent = line["fee_percent"]
        invoice_line.fee_amount = line["fee_amount"]
        invoice_line.charge_amount = line["charge_amount"] or "0"
        invoice_line.invoice_currency = line["invoice_currency"]
        invoice_line.exchange_rate = line["exchange_rate"] or ""
        invoice_line.exchange_rate_date = line["exchange_rate_date"]
        invoice_line.exchange_rate_source = line["exchange_rate_source"]
        invoice_line.source_sync = line["source_sync"]
        invoice_line.page_assets_snapshot = json.dumps(line["page_assets"], ensure_ascii=False)
        invoice_line.insert()

    return {
        "name": batch.name,
        "profile": batch.billing_profile,
        "customer": batch.billing_customer,
        "period_start": str(batch.period_start),
        "period_end": str(batch.period_end),
        "invoice_currency": batch.invoice_currency,
        "total": batch.total,
        "basis_total": batch.basis_total,
        "fee_total": batch.fee_total,
        "sales_invoice": batch.sales_invoice,
        "status": batch.status,
        "source_hash": batch.source_hash,
    }


def _sales_invoice_item(line, policy):
    """One ERPNext Sales Invoice line, for one source currency."""
    item = {
        "item_code": policy.get("invoice_item", ""),
        "qty": 1,
        "rate": line["charge_amount"] or "0",
        "cost_center": None,
    }
    if policy.get("income_account"):
        item["income_account"] = policy["income_account"]
    return item


def _tax_template(policy):
    """The tax template, when the profile's tax treatment says to apply one."""
    if policy.get("tax_treatment") == invoicing.TAX_TREATMENT_TEMPLATE:
        return policy.get("tax_template") or None
    return None


def _source_sync(lines):
    """The sync runs the charge came from."""
    runs = []
    for line in lines:
        run = line.get("source_sync") or ""
        if run and run not in runs:
            runs.append(run)
    return ", ".join(runs)


def _source_hash(group, lines, policy):
    """A fingerprint of the figures this invoice was created from.

    It answers one question: does the data still say what the invoice says?
    A re-sync that changes the spend changes this hash, and the difference is
    visible — instead of the approved period being quietly rewritten.
    """
    payload = json.dumps({
        "profile": group.get("name", ""),
        "customer": group.get("customer", ""),
        "fee_percent": group.get("fee_percent", ""),
        "invoice_currency": group.get("invoice_currency", ""),
        "charge_model": policy.get("charge_model", ""),
        "lines": [
            [line["account"], line["basis_currency"], line["basis_amount"], line["fee_amount"], line["charge_amount"]]
            for line in lines
        ],
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _group_key(billing_profile, billing_customer, fee_percent, invoice_currency):
    """The key of one billing group, as the spend report writes it."""
    profile = _text(billing_profile, "Billing profile", 140)
    customer = _text(billing_customer, "Billing customer", 140)
    try:
        fee = _decimal(fee_percent)
    except (InvalidOperation, TypeError, ValueError):
        frappe.throw("The service fee percentage must be a number.")
    currency = _text(invoice_currency, "Invoice currency", 3).upper()
    return "|".join((profile, customer, format(fee, "f"), currency))
