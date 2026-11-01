"""What may be INVOICED, and what must be decided before it is.

─── WHY THIS FILE EXISTS ──────────────────────────────────────────────────────

The brief asks for a customer invoice workspace: for each bill-to customer,
the rechargeable advertising cost, the calculated service fee, the billing
currency, the exchange-rate status, the tax treatment, what was invoiced
before, whether the period is ready, and the ERPNext Sales Invoice that
resulted.

It also asks for something the repository must not invent:

    "Do not assume whether LaraTik acts as principal or agent for advertising
     expenditure. This must remain configurable and subject to approved
     accounting policy."

So the vocabulary of an invoice — how much is charged, who recognises it, and
whether the period may be invoiced at all — is written down ONCE, here, and
the web application holds a mirror of it. `meta-ads-invoicing-parity-conformance.test.ts`
compares the two.

─── WHY IT HAS NO FRAPPE DEPENDENCY ───────────────────────────────────────────

Pure. No table, no connection, no framework import. That is what lets the
parity guard execute it directly, and what lets a reviewer read the rules
without standing up a site.

─── THE THREE UNRESOLVED POLICY DECISIONS, AS DATA ─────────────────────────────

The audit's §6 lists six policy decisions (P-1..P-6) that this repository
must not decide for the accountant. Three of them are properties of a BILLING
PROFILE, and this module names them as states the profile can be in:

| Decision | The question | The field's answer |
|---|---|---|
| **P-1** principal or agent | Is the recharge LaraTik's revenue, or money held for the customer? | `recognition_model` |
| **P-2** fee-only or fee-plus-recharge | Does the invoice carry the advertising cost, or the service fee alone? | `charge_model` |
| **P-3** income and expense accounts | Where is the charge booked? | `income_account` |

A profile that has NOT answered them is not broken — it is **undecided**, and
the invoice is not ready. That is the whole point: the surrounding
functionality (the workspace, the preview, the fee arithmetic, the currency
conversion) works perfectly well without a policy, and the ONE action that
needs a policy — creating the accounting document — waits.

─── WHY "BLOCKED" AND "NEEDS APPROVAL" ARE DIFFERENT ───────────────────────────

A **blocked** period cannot be invoiced because the DATA is not there: the
exchange rate is missing, the currency's precision is unknown, another invoice
already covers the period. No approval fixes those; the data must change.

A **needs-approval** period is waiting on a DECISION: the fee was never
configured, the charge model was never chosen, the tax treatment was never
stated. A human settles those, and the period becomes ready.

The draft action is enabled ONLY when the state is `ready`. Both other states
say exactly what is missing, so the reader is never left guessing which
button would work.

─── WHY THE FEE HAS A STATE AND NOT ONLY A NUMBER ──────────────────────────────

`LaraTik Meta Ads Billing Profile.fee_percent` has a default of `0`. So a
profile nobody ever configured reads as a zero fee, and a zero fee is a
legitimate answer — an approved zero. The two are indistinguishable, and a
customer would be billed nothing without anybody deciding anything.

The fee therefore has a STATE. A dated fee rule that says `0` is a
configured, approved zero. A base fee that says `0` is a configured, approved
zero ONLY when the profile says the fee is configured. Otherwise the fee is
**unconfigured**: not zero, not "no fee" — simply not stated, and the invoice
waits for it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal


# ─── THE CHARGE MODEL (P-2) ────────────────────────────────────────────────────

#: The invoice carries the advertising cost AND the service fee. LaraTik
#: advanced the money and recharges the customer.
CHARGE_MODEL_RECHARGE = "Recharge"

#: The invoice carries the service fee ALONE. The customer funded the
#: advertising, and LaraTik is billing for the service, not for the spend.
CHARGE_MODEL_SERVICE_FEE_ONLY = "Service fee only"

CHARGE_MODELS = (CHARGE_MODEL_RECHARGE, CHARGE_MODEL_SERVICE_FEE_ONLY)


# ─── THE RECOGNITION MODEL (P-1) ───────────────────────────────────────────────

#: LaraTik is the principal: the recharge is LaraTik's revenue.
RECOGNITION_PRINCIPAL = "Principal"

#: LaraTik is the agent: the fee is LaraTik's revenue, and the advertising
#: cost is the customer's money passing through.
RECOGNITION_AGENT = "Agent"

RECOGNITION_MODELS = (RECOGNITION_PRINCIPAL, RECOGNITION_AGENT)


# ─── THE TAX TREATMENT ─────────────────────────────────────────────────────────

#: No tax is applied to this invoice. The invoice carries no tax line.
TAX_TREATMENT_NONE = "None"

#: Tax is applied through an ERPNext Selling Taxes and Charges Template. The
#: profile must name the template; a treatment without a template is not a
#: decision, it is half of one.
TAX_TREATMENT_TEMPLATE = "Template"

TAX_TREATMENTS = (TAX_TREATMENT_NONE, TAX_TREATMENT_TEMPLATE)


# ─── THE FEE STATE ─────────────────────────────────────────────────────────────

#: Somebody stated the fee. It may be zero — an approved zero is a decision.
FEE_STATUS_CONFIGURED = "configured"

#: Nobody stated the fee. This is NOT zero.
FEE_STATUS_UNCONFIGURED = "unconfigured"

FEE_STATUSES = (FEE_STATUS_CONFIGURED, FEE_STATUS_UNCONFIGURED)


# ─── THE INVOICE READINESS STATES ──────────────────────────────────────────────

#: Everything needed is here. The draft may be created.
READINESS_READY = "ready"

#: A decision is missing. A human settles it and the period becomes ready.
READINESS_NEEDS_APPROVAL = "needs_approval"

#: The data is not there. No approval fixes it; the data must change.
READINESS_BLOCKED = "blocked"

#: This period was already invoiced. It is never invoiced twice.
READINESS_ALREADY_INVOICED = "already_invoiced"

#: There is nothing to charge. Not an error — an empty period.
READINESS_NOTHING_TO_INVOICE = "nothing_to_invoice"

READINESS_STATES = (
    READINESS_READY,
    READINESS_NEEDS_APPROVAL,
    READINESS_BLOCKED,
    READINESS_ALREADY_INVOICED,
    READINESS_NOTHING_TO_INVOICE,
)


# ─── THE READINESS REASONS ─────────────────────────────────────────────────────

#: Nobody ever stated what the fee is. Not zero — UNSTATED.
NEEDS_APPROVAL_REASON_FEE_UNCONFIGURED = "fee_unconfigured"

#: Nobody chose between a recharge and a fee-only invoice.
NEEDS_APPROVAL_REASON_CHARGE_MODEL_NOT_DECIDED = "charge_model_not_decided"

#: Nobody said whether LaraTik is the principal or the agent.
NEEDS_APPROVAL_REASON_RECOGNITION_MODEL_NOT_DECIDED = "recognition_model_not_decided"

#: No income account was named, so the charge has nowhere to be booked.
NEEDS_APPROVAL_REASON_INCOME_ACCOUNT_MISSING = "income_account_missing"

#: No Item was named, so the invoice has no line to bill.
NEEDS_APPROVAL_REASON_INVOICE_ITEM_MISSING = "invoice_item_missing"

#: Nobody stated the tax treatment.
NEEDS_APPROVAL_REASON_TAX_TREATMENT_NOT_DECIDED = "tax_treatment_not_decided"

#: A tax TEMPLATE was chosen but the template itself was not named.
NEEDS_APPROVAL_REASON_TAX_TEMPLATE_MISSING = "tax_template_missing"

#: A COMMERCIAL or ACCOUNTING term was never stated. A human settles it.
NEEDS_APPROVAL_REASONS = (
    NEEDS_APPROVAL_REASON_FEE_UNCONFIGURED,
    NEEDS_APPROVAL_REASON_CHARGE_MODEL_NOT_DECIDED,
    NEEDS_APPROVAL_REASON_RECOGNITION_MODEL_NOT_DECIDED,
    NEEDS_APPROVAL_REASON_INCOME_ACCOUNT_MISSING,
    NEEDS_APPROVAL_REASON_INVOICE_ITEM_MISSING,
    NEEDS_APPROVAL_REASON_TAX_TREATMENT_NOT_DECIDED,
    NEEDS_APPROVAL_REASON_TAX_TEMPLATE_MISSING,
)

#: No approved exchange rate exists for the conversion.
BLOCKED_REASON_EXCHANGE_RATE_MISSING = "exchange_rate_missing"

#: The currency's precision is unknown, so the amount cannot be rounded.
BLOCKED_REASON_CURRENCY_PRECISION_UNKNOWN = "currency_precision_unknown"

#: Another invoice already covers part of this period.
BLOCKED_REASON_PERIOD_OVERLAPS_AN_INVOICE = "period_overlaps_an_invoice"

#: The spend cannot be traced to a billable advertiser.
BLOCKED_REASON_SPEND_NOT_ATTRIBUTABLE = "spend_not_attributable"

#: The customer has no receivable account at all, in this company.
BLOCKED_REASON_RECEIVABLE_ACCOUNT_MISSING = "receivable_account_missing"

#: The customer's receivable account is in ANOTHER currency than the invoice.
#: ERPNext refuses such an invoice ("Party Account ... currency ... and document
#: currency ... should be same"), so the site has to be set up before the
#: period can be invoiced.
BLOCKED_REASON_RECEIVABLE_ACCOUNT_CURRENCY_MISMATCH = "receivable_account_currency_mismatch"

#: The DATA is not there. Nothing to approve.
BLOCKED_REASONS = (
    BLOCKED_REASON_EXCHANGE_RATE_MISSING,
    BLOCKED_REASON_CURRENCY_PRECISION_UNKNOWN,
    BLOCKED_REASON_PERIOD_OVERLAPS_AN_INVOICE,
    BLOCKED_REASON_SPEND_NOT_ATTRIBUTABLE,
    BLOCKED_REASON_RECEIVABLE_ACCOUNT_MISSING,
    BLOCKED_REASON_RECEIVABLE_ACCOUNT_CURRENCY_MISMATCH,
)

READINESS_REASONS = NEEDS_APPROVAL_REASONS + BLOCKED_REASONS + ("already_invoiced", "no_chargeable_spend")


# ─── THE READINESS RULE ────────────────────────────────────────────────────────

#: The ordered table of rules. Each names the state it produces and the
#: evidence it needs. The order decides the answer:
#:
#: 1. already_invoiced — an invoiced period is never invoiced again, whatever
#:    else has changed since. This is the same guarantee ADR-205 gives the
#:    snapshot, applied to the invoice.
#: 2. nothing_to_invoice — there is no chargeable spend, so there is nothing
#:    to decide. A period with no money in it must not report a missing fee.
#: 3. blocked — the data is not there.
#: 4. needs_approval — a decision is missing.
#: 5. ready.
READINESS_RULES = (
    {"id": "already_invoiced", "state": READINESS_ALREADY_INVOICED, "evidence": "the period was already invoiced"},
    {"id": "nothing_to_invoice", "state": READINESS_NOTHING_TO_INVOICE, "evidence": "there is no chargeable spend"},
    {"id": "blocked", "state": READINESS_BLOCKED, "evidence": "the data is missing"},
    {"id": "needs_approval", "state": READINESS_NEEDS_APPROVAL, "evidence": "a decision is missing"},
    {"id": "ready", "state": READINESS_READY, "evidence": "everything needed is here"},
)


def classify_readiness(checks):
    """Name the readiness of one invoice period, and say why.

    `checks` is a mapping of the facts:

    {
        "already_invoiced": bool,
        "chargeable_spend": Decimal | int | str,   # the basis that would be charged
        "blocked_reasons": [...],                   # from BLOCKED_REASONS
        "approval_reasons": [...],                 # from NEEDS_APPROVAL_REASONS
    }

    Returns (state, reasons) where `reasons` is the ordered list of reasons
    that produced the state.
    """
    blocked = list(checks.get("blocked_reasons") or [])
    approvals = list(checks.get("approval_reasons") or [])
    spend = Decimal(str(checks.get("chargeable_spend") or 0))

    if checks.get("already_invoiced"):
        return READINESS_ALREADY_INVOICED, ["already_invoiced"]
    if spend <= 0:
        return READINESS_NOTHING_TO_INVOICE, ["no_chargeable_spend"]
    if blocked:
        return READINESS_BLOCKED, blocked
    if approvals:
        return READINESS_NEEDS_APPROVAL, approvals
    return READINESS_READY, []


# ─── THE FEE ───────────────────────────────────────────────────────────────────

def resolve_fee(rules, base_percent, fee_is_configured, on_date):
    """The fee for a spend date, AND whether anybody decided it.

    `rules` is a list of `{"effective_from": date, "fee_percent": Decimal}`
    — the profile's dated fee rules. `base_percent` is the profile's standing
    fee. `fee_is_configured` says whether the standing fee was actually
    stated.

    Returns (status, percent, source):
      - status is one of `FEE_STATUSES`;
      - percent is the fee as a Decimal, or "" when nothing was stated;
      - source is "dated rule", "profile", or "".

    A DATED RULE that says zero is a configured, approved zero: somebody
    wrote the rule down. The standing fee is used only when the profile says
    it was configured, because a profile nobody touched reads as zero and
    that zero was never anybody's decision.

    ─── WHY A NON-ZERO BASE FEE IS CONFIGURED, WHATEVER THE FLAG SAYS ──────────

    The flag exists to tell an APPROVED ZERO apart from NO FEE AT ALL. It is
    not the only way a fee gets stated: a profile whose standing fee is 15%
    had its fee stated by somebody typing 15, whether or not that somebody
    also ticked a flag added later.

    So the base fee counts as configured when it is NON-ZERO, and the flag
    only decides the ZERO case:

        base 15, flag off  → configured 15%   (a fee was stated)
        base 0,  flag off  → unconfigured     (nobody ever stated a fee)
        base 0,  flag on   → configured 0%    (an approved zero)

    Reading it the other way — the flag deciding on its own — would silently
    zero every fee on every profile that predates the flag, which is the
    worst possible way to be wrong about money.
    """
    applicable = []
    for rule in rules or []:
        effective_from = rule.get("effective_from")
        if effective_from is None:
            continue
        if isinstance(effective_from, str):
            effective_from = date.fromisoformat(effective_from[:10])
        if effective_from <= on_date:
            applicable.append((effective_from, Decimal(str(rule.get("fee_percent") or 0))))
    if applicable:
        effective_from, percent = max(applicable, key=lambda item: item[0])
        return FEE_STATUS_CONFIGURED, percent, "dated rule"
    base = Decimal(str(base_percent or 0))
    if fee_is_configured or base != 0:
        return FEE_STATUS_CONFIGURED, base, "profile"
    return FEE_STATUS_UNCONFIGURED, Decimal(0), ""


# ─── THE CHARGE ────────────────────────────────────────────────────────────────

def charge_amount(charge_model, basis, fee):
    """What the invoice carries, for a given charge model.

    `basis` is the advertising cost in the ACCOUNT's currency. `fee` is the
    calculated service fee in the same currency.

    A RECHARGE invoice carries the advertising cost AND the fee: LaraTik
    advanced the money, and the customer pays it back with the fee on top.
    A SERVICE-FEE-ONLY invoice carries the fee alone: the customer funded the
    advertising, and the spend is the customer's money passing through — so
    putting it on the invoice would bill the customer for their own spend.

    The models are NOT the same number, and the difference is the whole
    policy question (P-2). This function is the arithmetic of the answer the
    profile gives; it never chooses the answer.
    """
    basis = Decimal(str(basis))
    fee = Decimal(str(fee))
    if charge_model == CHARGE_MODEL_RECHARGE:
        return basis + fee
    if charge_model == CHARGE_MODEL_SERVICE_FEE_ONLY:
        return fee
    raise ValueError("The charge model is not decided, so the charge cannot be computed.")


def fee_amount(basis, fee_percent):
    """The service fee on a basis, at a stated percentage."""
    return Decimal(str(basis)) * Decimal(str(fee_percent)) / Decimal(100)
