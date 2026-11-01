"""Converting an ERPNext invoice's `outstanding_amount` to company currency.

─── WHY THIS FILE EXISTS ─────────────────────────────────────────────────────

The Meta Ads workspace asked ERPNext for what the customers still owe, and it
read a field ERPNext does not have: `base_outstanding_amount`. ERPNext 16 has
no such column, so the query failed on a real site — and it failed only on a
real site, because every other check in this repository reads a mocked
Frappe surface that answers whatever the fixture says.

The rule that fixes it is not new. It is **ADR-017's**, and the application
already implements it, in TypeScript, in
`cashbox-app/src/lib/erpnext/financials/invoice-amount.ts` — for the `/position`
screen's receivables. So this module is a MIRROR of that rule, not a second
opinion about it, and the two are held together by a parity guard.

─── THE FIELDS, AND WHICH CURRENCY EACH ONE IS IN ──────────────────────────────

Verified against `frappe/erpnext` `version-15` and against a live ERPNext
16.50.0 site:

| Value | Where ERPNext sets it | Currency it is in |
|---|---|---|
| `outstanding_amount` | `accounts/utils.py` — `outstanding["outstanding_in_account_currency"]` | **party account** |
| `conversion_rate` | `accounts_controller.py` — `get_exchange_rate(currency, company)` | **transaction → company** |

The invoice proves the first: the controller writes
`reconcilation_entry.credit_in_account_currency = self.outstanding_amount` and
converts to company currency with a SEPARATELY fetched rate, because the
invoice does not carry the party-account rate.

**There is no `base_outstanding_amount`.** A figure that cannot be taken
pre-converted has to be converted by the rule that knows which rate applies.

─── THE RULE ──────────────────────────────────────────────────────────────────

1. The party account currency is the company currency → the amount is ALREADY
   in company currency, and the factor is **1**. Applying `conversion_rate`
   here is the bug: it is the transaction→company rate, and it has nothing to
   do with this amount.
2. The party account currency is the TRANSACTION currency → the amount is in
   the billing currency, and the factor is `conversion_rate`.
3. Anything else needs a party-account→company rate this app does not have,
   and it is **reported as unresolved**, never guessed.

─── WHY IT IS PURE ────────────────────────────────────────────────────────────

No `import frappe`. The rule is arithmetic over five values, and a rule that
needs a live site can only be tested on a live site — which is how a rule that
was wrong for a year stayed wrong. This module is testable without a bench,
and the parity guard compares it with the TypeScript that the `/position`
screen already uses.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import NamedTuple, Optional

#: The basis on which a converted amount was obtained.
BASIS_IDENTITY = "identity"
BASIS_CONVERSION_RATE = "conversion_rate"

#: WHY a figure could not be stated in company currency.
REASON_MISSING_OUTSTANDING_AMOUNT = "missing_outstanding_amount"
REASON_NON_NUMERIC_OUTSTANDING = "non_numeric_outstanding"
REASON_MISSING_PARTY_ACCOUNT_CURRENCY = "missing_party_account_currency"
REASON_MISSING_CONVERSION_RATE = "missing_conversion_rate"
REASON_NO_RATE_FOR_PARTY_ACCOUNT_CURRENCY = "no_rate_for_party_account_currency"
REASON_MISSING_COMPANY_CURRENCY = "missing_company_currency"

#: The ERPNext `Account` field that names the currency the account holds.
#: NOT `currency` — the doctype has no field by that name.
ACCOUNT_CURRENCY_FIELD = "account_currency"

#: The ERPNext `Company` field that names the company's currency.
COMPANY_CURRENCY_FIELD = "default_currency"


class Conversion(NamedTuple):
    """The outcome of one conversion.

    `amount` is `None` when the figure could not be stated, and `reason` says
    why. An absent amount is NEVER a zero: a receivable nobody could read is
    a receivable that is missing from the books, and reporting it as zero is
    how a shortfall hides.
    """

    status: str
    amount: Optional[Decimal] = None
    factor: Optional[Decimal] = None
    basis: Optional[str] = None
    reason: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _currency(value: Optional[str]) -> str:
    """A currency code, trimmed and upper-cased. Empty means UNKNOWN."""
    if not isinstance(value, str):
        return ""
    return value.strip().upper()


def _decimal(value) -> Optional[Decimal]:
    """Read a decimal, or `None` when the value is not one."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def read_decimal(value) -> Optional[Decimal]:
    """Read an amount, or `None` when the value is not a number.

    The public reader for the amount ERPNext keeps. A caller that needs the
    raw amount — to group it by the currency it is denominated in — reads it
    HERE rather than reaching for a second parser with its own idea of what a
    number is.
    """
    return _decimal(value)


def to_company_currency(
    outstanding,
    party_account_currency: Optional[str],
    invoice_currency: Optional[str] = None,
    company_currency: Optional[str] = None,
    conversion_rate=None,
) -> Conversion:
    """Convert one invoice's outstanding amount to company currency.

    The rule is ADR-017's, and the order of the checks is the TypeScript's:
    the transaction-currency case is decided FIRST, because an invoice whose
    party account is in the billing currency converts by `conversion_rate`,
    and an invoice whose party account is in the company currency is already
    converted. Deciding them in the other order would multiply a company
    currency amount by a rate that does not apply to it.

    :param outstanding: the invoice's `outstanding_amount`, in the PARTY
        ACCOUNT's currency.
    :param party_account_currency: the currency of the invoice's receivable
        account — `Account.account_currency`.
    :param invoice_currency: the invoice's billing currency.
    :param company_currency: the company's `Company.default_currency`.
    :param conversion_rate: the invoice's `conversion_rate`, transaction →
        company.
    """
    if outstanding is None or (isinstance(outstanding, str) and outstanding.strip() == ""):
        # A BLANK amount is not a zero. An amount nobody could read is an
        # amount that is unknown, and an unknown amount is not a zero
        # receivable — it is a receivable that is missing from the books.
        return Conversion(
            "unresolved",
            reason=REASON_MISSING_OUTSTANDING_AMOUNT,
            detail=(
                "The invoice does not report an outstanding amount, so what the "
                "client owes on it is unknown rather than zero."
            ),
        )

    amount = _decimal(outstanding)
    if amount is None:
        return Conversion(
            "unresolved",
            reason=REASON_NON_NUMERIC_OUTSTANDING,
            detail="The invoice's outstanding amount is not a number.",
        )

    party_account = _currency(party_account_currency)
    if not party_account:
        return Conversion(
            "unresolved",
            reason=REASON_MISSING_PARTY_ACCOUNT_CURRENCY,
            detail=(
                "The invoice does not report its party account currency, so the "
                "currency its outstanding amount is denominated in is unknown."
            ),
        )

    billing = _currency(invoice_currency)

    if billing and billing == party_account:
        # The common case: the amount is in the billing currency, and
        # `conversion_rate` is the billing → company rate.
        rate = _decimal(conversion_rate)
        if rate is None or rate <= 0:
            return Conversion(
                "unresolved",
                reason=REASON_MISSING_CONVERSION_RATE,
                detail="The invoice's conversion rate cannot be used as a factor.",
            )
        return Conversion(
            "ok",
            amount=(amount * rate).quantize(Decimal("0.01")),
            factor=rate,
            basis=BASIS_CONVERSION_RATE,
        )

    # From here the account currency is not the billing currency, so
    # `conversion_rate` is the WRONG rate and must not be applied.
    company = _currency(company_currency)
    if company and party_account == company:
        return Conversion(
            "ok",
            amount=amount.quantize(Decimal("0.01")),
            factor=Decimal(1),
            basis=BASIS_IDENTITY,
        )

    return Conversion(
        "unresolved",
        reason=(
            REASON_NO_RATE_FOR_PARTY_ACCOUNT_CURRENCY
            if company
            else REASON_MISSING_COMPANY_CURRENCY
        ),
        detail=(
            "The invoice is billed in {0} but its party account is in {1}, and the "
            "{1} → {2} rate is not on the invoice.".format(
                billing or "an unstated currency",
                party_account,
                company or "company currency",
            )
        ),
    )


def convert_all(rows) -> "tuple[list, list, Optional[Decimal]]":
    """Convert a list of rows, and refuse to return a PARTIAL total.

    :param rows: dicts with the keys `outstanding`, `party_account_currency`,
        `invoice_currency`, `company_currency` and `conversion_rate`.
    :returns: `(converted, unresolved, total)` — the converted rows with their
        company-currency amounts, the rows that could not be stated with their
        reasons, and the total. The total is `None` whenever anything is
        unresolved, because a partial total is smaller than the truth and
        nothing on screen says so.
    """
    converted = []
    unresolved = []
    total = Decimal(0)
    for index, row in enumerate(rows):
        result = to_company_currency(
            row.get("outstanding"),
            row.get("party_account_currency"),
            row.get("invoice_currency"),
            row.get("company_currency"),
            row.get("conversion_rate"),
        )
        if result.ok:
            converted.append((index, result.amount, result.basis, result.factor))
            total += result.amount
        else:
            unresolved.append((index, result.reason, result.detail))
    return converted, unresolved, (None if unresolved else total)
