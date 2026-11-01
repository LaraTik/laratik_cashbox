"""The SIX STATES of Meta advertising spend, and the one rule that names them.

─── WHY THIS FILE EXISTS ──────────────────────────────────────────────────────

The brief asks for six things to be told apart, and it says explicitly:

    "Do not treat all unassigned spend as financially equivalent to
     intentionally non-billable spend."

Before, there were three buckets. Attributed spend was charged. Everything else
was "unassigned". So a Page whose payer arrangement had been SWITCHED OFF came
back as unassigned — the same bucket, and the same number, as a Page nobody
had ever mapped. One is a decision somebody made; the other is a gap nobody
has filled. They are not financially equivalent, and a report that adds them
together cannot answer "how much am I failing to bill?" — because the answer
is already mixed into the failure.

So the six states are named ONCE, here, and the rule that chooses between them
is written ONCE, here. The web application holds a mirror of both, and
`meta-ads-chargeability-parity-conformance.test.ts` runs one against the other
so the two cannot drift apart.

─── WHY IT HAS NO FRAPPE DEPENDENCY ───────────────────────────────────────────

This module is PURE. It reads no table, holds no connection, and imports
nothing from the framework. That is not tidiness — it is what lets the parity
guard execute it directly, and what lets a reviewer read the rule without
standing up a site.

─── THE ORDER OF THE STATES IS THE BRIEF'S ORDER ──────────────────────────────

`CHARGEABILITY_STATES` is ordered exactly as the brief lists them, and the
order is the READING order: money that can be billed, money that is
deliberately not billed, money that is not yet attributable, money that is
ambiguous, money already invoiced, and money that cannot be invoiced yet.

Anything that displays these states — the Overview legend, the attribution
queue, the invoice readiness check — displays them in this order, so the
reader sees the same sequence wherever the states appear.
"""

from __future__ import annotations


# ─── THE STATES ────────────────────────────────────────────────────────────────

CHARGEABILITY_ASSIGNED_BILLABLE = "assigned_billable"
CHARGEABILITY_ASSIGNED_NOT_BILLABLE = "assigned_not_billable"
CHARGEABILITY_UNASSIGNED = "unassigned"
CHARGEABILITY_AMBIGUOUS = "ambiguous"
CHARGEABILITY_ALREADY_INVOICED = "already_invoiced"
CHARGEABILITY_BLOCKED = "blocked"

CHARGEABILITY_STATES = (
    CHARGEABILITY_ASSIGNED_BILLABLE,
    CHARGEABILITY_ASSIGNED_NOT_BILLABLE,
    CHARGEABILITY_UNASSIGNED,
    CHARGEABILITY_AMBIGUOUS,
    CHARGEABILITY_ALREADY_INVOICED,
    CHARGEABILITY_BLOCKED,
)

# ─── THE REASONS ───────────────────────────────────────────────────────────────
#
# A state says WHICH bucket. A reason says WHY, in words a human can act on.
# The reason vocabulary is CLOSED: an unrecognised reason is reported as such
# rather than passed through, because a reason string that appears on screen
# must be one the interface can translate.

CHARGEABILITY_REASON_MATCHED_PAGE_AND_PROFILE = "matched_page_and_profile"
CHARGEABILITY_REASON_PROFILE_SWITCHED_OFF = "profile_switched_off"
CHARGEABILITY_REASON_NO_PAGE_MAPPING = "no_page_mapping"
CHARGEABILITY_REASON_UNRECOGNISED_ATTRIBUTION_STATUS = "unrecognised_attribution_status"
CHARGEABILITY_REASON_AMBIGUOUS_PAGE_IDENTITY = "ambiguous_page_identity"
CHARGEABILITY_REASON_INVOICE_SOURCE_TRACKED = "invoice_source_tracked"
CHARGEABILITY_REASON_BLOCKED_REASON = "blocked_reason"
CHARGEABILITY_REASON_EXCHANGE_RATE_MISSING = "exchange_rate_missing"
CHARGEABILITY_REASON_BILLING_CURRENCY_MISSING = "billing_currency_missing"
CHARGEABILITY_REASON_CURRENCY_PRECISION_UNKNOWN = "currency_precision_unknown"
CHARGEABILITY_REASON_FEE_NOT_CONFIGURED = "fee_not_configured"
CHARGEABILITY_REASON_PERIOD_CLOSED = "period_closed"
CHARGEABILITY_REASON_BILL_TO_MISSING = "bill_to_missing"

CHARGEABILITY_REASONS = {
    CHARGEABILITY_ASSIGNED_BILLABLE: (CHARGEABILITY_REASON_MATCHED_PAGE_AND_PROFILE,),
    CHARGEABILITY_ASSIGNED_NOT_BILLABLE: (CHARGEABILITY_REASON_PROFILE_SWITCHED_OFF,),
    CHARGEABILITY_UNASSIGNED: (
        CHARGEABILITY_REASON_NO_PAGE_MAPPING,
        CHARGEABILITY_REASON_UNRECOGNISED_ATTRIBUTION_STATUS,
    ),
    CHARGEABILITY_AMBIGUOUS: (CHARGEABILITY_REASON_AMBIGUOUS_PAGE_IDENTITY,),
    CHARGEABILITY_ALREADY_INVOICED: (CHARGEABILITY_REASON_INVOICE_SOURCE_TRACKED,),
    CHARGEABILITY_BLOCKED: (
        CHARGEABILITY_REASON_BLOCKED_REASON,
        CHARGEABILITY_REASON_EXCHANGE_RATE_MISSING,
        CHARGEABILITY_REASON_BILLING_CURRENCY_MISSING,
        CHARGEABILITY_REASON_CURRENCY_PRECISION_UNKNOWN,
        CHARGEABILITY_REASON_FEE_NOT_CONFIGURED,
        CHARGEABILITY_REASON_PERIOD_CLOSED,
        CHARGEABILITY_REASON_BILL_TO_MISSING,
    ),
}

# ─── THE ATTRIBUTION STATUSES THE SYNC STORES ──────────────────────────────────
#
# These are the values `LaraTik Meta Ads Spend.attribution_status` carries.
# They are the EVIDENCE the rule reads; the six states are the VERDICT.

ATTRIBUTION_STATUS_ATTRIBUTED = "Attributed"
ATTRIBUTION_STATUS_UNMAPPED = "Unmapped"
ATTRIBUTION_STATUS_AMBIGUOUS = "Ambiguous"

ATTRIBUTION_STATUSES = (
    ATTRIBUTION_STATUS_ATTRIBUTED,
    ATTRIBUTION_STATUS_UNMAPPED,
    ATTRIBUTION_STATUS_AMBIGUOUS,
)

# ─── THE RULE ──────────────────────────────────────────────────────────────────
#
# An ordered table. The FIRST rule whose predicate holds decides the state, so
# the order IS the precedence, and it is the precedence the brief implies:
#
#   1. already invoiced  — the money has been billed; nothing else matters.
#   2. ambiguous         — we do not know whose it is, so nothing else can be
#                          said about it.
#   3. switched off      — a DECISION. It outranks a chargeability defect,
#                          because a switched-off arrangement is not waiting
#                          for anything.
#   4. unassigned        — nobody's. There is no payer to be blocked for.
#   5. blocked           — we know whose it is, and something stops the
#                          invoice. This is the only state a fee or an
#                          exchange rate can produce.
#   6. assigned, billable— the ordinary case.
#
# Rule 1 comes before rule 3 and rule 5 so that an APPROVED period is never
# re-classified by a later change: once the lines are on an invoice, they are
# on an invoice, whatever the arrangement does afterwards. That is the same
# guarantee ADR-205 gives the money path, applied to the reporting path.
#
# Rule 4 comes before rule 5 for the same kind of reason: "blocked from
# invoicing" describes a line that could be invoiced but cannot be yet, and a
# line with no payer is not that — it is unassigned, which is its own answer
# and its own work item.

CHARGEABILITY_RULES = (
    ("has_invoice_source", CHARGEABILITY_ALREADY_INVOICED, CHARGEABILITY_REASON_INVOICE_SOURCE_TRACKED),
    ("is_ambiguous", CHARGEABILITY_AMBIGUOUS, CHARGEABILITY_REASON_AMBIGUOUS_PAGE_IDENTITY),
    ("is_switched_off", CHARGEABILITY_ASSIGNED_NOT_BILLABLE, CHARGEABILITY_REASON_PROFILE_SWITCHED_OFF),
    ("is_unmapped", CHARGEABILITY_UNASSIGNED, CHARGEABILITY_REASON_NO_PAGE_MAPPING),
    ("has_blocked_reason", CHARGEABILITY_BLOCKED, "$blocked_reason"),
    ("is_attributed", CHARGEABILITY_ASSIGNED_BILLABLE, CHARGEABILITY_REASON_MATCHED_PAGE_AND_PROFILE),
)

# The rule the table does not cover: an attribution status nobody recognises.
# It is reported as unassigned, with a reason that says the status was not
# understood — because a reader who sees "no page mapping" would go looking
# for a mapping that is not the problem.
CHARGEABILITY_FALLBACK = (
    CHARGEABILITY_UNASSIGNED,
    CHARGEABILITY_REASON_UNRECOGNISED_ATTRIBUTION_STATUS,
)

# ─── THE EVIDENCE ──────────────────────────────────────────────────────────────
#
# What the rule reads. It is deliberately small and total: every field is
# always present, and an absent field has ONE meaning.
#
#   attribution_status  the stored sync verdict
#   switched_off        the owning arrangement is switched off, so this spend
#                       was never charged. It is TRUE only where the sync
#                       stored the row as Unmapped BECAUSE the arrangement was
#                       off — never for a row the sync charged, so an approved
#                       period cannot be reclassified by a later change.
#   invoice_source      the source lines are already on an ERPNext draft
#   blocked_reason      why this spend cannot be invoiced yet

CHARGEABILITY_EVIDENCE_FIELDS = (
    "attribution_status",
    "switched_off",
    "invoice_source",
    "blocked_reason",
)

# The reasons that may arrive as `blocked_reason`. A blocked reason outside
# this set is reported as the generic `blocked_reason` — the state is still
# right, and the specific cause is still shown by the screen that raised it.
CHARGEABILITY_BLOCKED_REASONS = (
    CHARGEABILITY_REASON_EXCHANGE_RATE_MISSING,
    CHARGEABILITY_REASON_BILLING_CURRENCY_MISSING,
    CHARGEABILITY_REASON_CURRENCY_PRECISION_UNKNOWN,
    CHARGEABILITY_REASON_FEE_NOT_CONFIGURED,
    CHARGEABILITY_REASON_PERIOD_CLOSED,
    CHARGEABILITY_REASON_BILL_TO_MISSING,
)


def _evidence(evidence=None, **overrides):
    """Normalise an evidence record, so a missing field has ONE meaning."""
    record = {
        "attribution_status": "",
        "switched_off": False,
        "invoice_source": "",
        "blocked_reason": "",
    }
    if isinstance(evidence, dict):
        record.update(evidence)
    record.update(overrides)
    record["attribution_status"] = str(record.get("attribution_status") or "")
    record["switched_off"] = bool(record.get("switched_off"))
    record["invoice_source"] = str(record.get("invoice_source") or "")
    record["blocked_reason"] = str(record.get("blocked_reason") or "")
    return record


def _has_invoice_source(record):
    return bool(record["invoice_source"])


def _is_ambiguous(record):
    return record["attribution_status"] == ATTRIBUTION_STATUS_AMBIGUOUS


def _is_switched_off(record):
    return record["switched_off"]


def _is_unmapped(record):
    return record["attribution_status"] == ATTRIBUTION_STATUS_UNMAPPED


def _has_blocked_reason(record):
    return bool(record["blocked_reason"])


def _is_attributed(record):
    return record["attribution_status"] == ATTRIBUTION_STATUS_ATTRIBUTED


_PREDICATES = {
    "has_invoice_source": _has_invoice_source,
    "is_ambiguous": _is_ambiguous,
    "is_switched_off": _is_switched_off,
    "is_unmapped": _is_unmapped,
    "has_blocked_reason": _has_blocked_reason,
    "is_attributed": _is_attributed,
}


def classify_chargeability(evidence=None, **overrides):
    """Return ``(state, reason)`` for one piece of Meta advertising spend.

    The state is one of `CHARGEABILITY_STATES` and the reason is one of the
    reasons `CHARGEABILITY_REASONS` allows for that state. Neither is ever
    invented: a state this function cannot justify is not returned.
    """
    record = _evidence(evidence, **overrides)
    for predicate_name, state, reason in CHARGEABILITY_RULES:
        predicate = _PREDICATES.get(predicate_name)
        if predicate is None or not predicate(record):
            continue
        if reason == "$blocked_reason":
            candidate = record["blocked_reason"]
            reason = candidate if candidate in CHARGEABILITY_BLOCKED_REASONS else CHARGEABILITY_REASON_BLOCKED_REASON
        return state, reason
    return CHARGEABILITY_FALLBACK


def is_billable_state(state):
    """Is this the state whose spend is charged to a customer?"""
    return state == CHARGEABILITY_ASSIGNED_BILLABLE
