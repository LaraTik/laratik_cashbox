"""What a Meta billing CSV line IS, and what an import may do with it.

─── WHY THIS FILE EXISTS ──────────────────────────────────────────────────────

The brief asks for a guided import that can tell the operator, before anything
is stored:

    "Validate dates, currency, amounts, and record types."
    "Detect duplicates and invalid records."
    "Support payment, refund, credit, and reversal records without incorrectly
     treating them as customer payments."

The last one is the load-bearing sentence, and it was NOT true before.

A Meta invoice-summary row carries a *payment method* — "Visa", "Ad Credit",
"Reversal" — and the kind of the record was inferred from it. The old inference
knew three names and one fallback:

    credit tokens         -> Credit
    "refund" or amount<0 -> Refund
    payment-method tokens -> Payment
    anything else         -> Unclassified

A row whose method is "Reversal" therefore fell into one of two wrong buckets.
A NEGATIVE reversal was labelled a Refund — which says we refunded money,
when what actually happened is that Meta undid a charge. A POSITIVE reversal
was labelled Unclassified — which says we do not know what it is, when the
file said exactly what it was. Both are financial misstatements, and the second
one hides inside the "needs review" bucket where nobody looks.

So the vocabulary gains a name, and the rule that chooses between names is
written ONCE, here. The web application holds a mirror of the vocabulary, and
`meta-ads-import-parity-conformance.test.ts` compares the two so the app can
never be missing a kind the server can produce.

─── WHY IT HAS NO FRAPPE DEPENDENCY ───────────────────────────────────────────

This module is PURE. It reads no table and imports nothing from the framework.
That is what lets the parity guard execute it directly, and what lets a
reviewer read the rules without standing up a site.

─── THE ORDER OF THE RULES IS LOAD-BEARING ────────────────────────────────────

`RECORD_KIND_RULES` is an ORDERED table, and the order decides the answer:

  1. REVERSAL   before everything. The word "reversal" is an explicit
                statement that the row UNDOES a prior document. A method like
                "Ad Credit Reversal" or "Credit Card Reversal" contains a
                credit token and a payment token as well — but it is still a
                reversal, and calling it a credit would be calling it the
                opposite of what it is.
  2. CREDIT     before the refund and amount rules, because a promotional
                credit is a credit even when the export carries it negative.
  3. REFUND     by name, then by sign.
  4. PAYMENT    by payment-method tokens.
  5. UNCLASSIFIED. The honest fallback: the file did not say, and neither
                will this module.

─── WHY UNCLASSIFIED IS NOT AN ERROR ──────────────────────────────────────────

A row that says "Money" is not a broken row. It is a row whose direction this
module cannot state, and the brief's own words are that such a record must
not be treated as a customer payment. So it is neither a payment nor a
refund; it is carried with its own sign and flagged for a human.

─── WHY THE SETTLEMENT FORMULA LIVES HERE ─────────────────────────────────────

The net settlement position is money: payments LESS refunds, credits and
reversals, PLUS whatever was unclassified (which keeps its own sign, because
its direction is not known). Three things reduce the position and one adds
to it, and getting that wrong is a sign error on a bank balance.

The formula is written once here, mirrored in the app, and checked by the
parity guard — because a settlement figure is the kind of number nobody
re-reads twice.
"""

from __future__ import annotations

from decimal import Decimal


# ─── THE RECORD KINDS ──────────────────────────────────────────────────────────

IMPORT_KIND_PAYMENT = "Payment"
IMPORT_KIND_REFUND = "Refund"
IMPORT_KIND_CREDIT = "Credit"
IMPORT_KIND_REVERSAL = "Reversal"
IMPORT_KIND_UNCLASSIFIED = "Unclassified"

#: Every kind a stored Meta billing row can carry, in settlement reading order:
#: money in, then money back, then the ones nobody can place.
IMPORT_RECORD_KINDS = (
    IMPORT_KIND_PAYMENT,
    IMPORT_KIND_REFUND,
    IMPORT_KIND_CREDIT,
    IMPORT_KIND_REVERSAL,
    IMPORT_KIND_UNCLASSIFIED,
)

#: The kinds whose total is stated as a MAGNITUDE and SUBTRACTED from the
#: settlement position. A refund, a credit and a reversal all move money back
#: towards LaraTik, so none of them increases what we owe Meta.
SETTLEMENT_REDUCING_KINDS = (
    IMPORT_KIND_REFUND,
    IMPORT_KIND_CREDIT,
    IMPORT_KIND_REVERSAL,
)

#: The kinds whose total is stated as a MAGNITUDE and ADDED to the settlement
#: position.
SETTLEMENT_INCREASING_KINDS = (IMPORT_KIND_PAYMENT,)


# ─── THE TOKENS ───────────────────────────────────────────────────────────────

#: A method that says "reversal" says the row undoes a prior document.
#:
#: A chargeback is deliberately NOT here: a chargeback is a card dispute, not
#: a Meta billing reversal, and naming it one would be inventing an event the
#: file did not describe.
REVERSAL_TOKENS = (
    "reversal",
    "reverse charge",
    "reversed",
)

#: A method that NAMES a credit. "Ad Credit" is Meta's promotional credit.
CREDIT_TOKENS = (
    "ad credit",
    "promotional credit",
    "account credit",
)

#: The bare word "credit" also names a credit — but only when the method is not
#: a CARD name. "Credit Card" is a card payment, and a rule that matched on
#: the bare word would turn every Visa row into a credit.
CREDIT_WORD = "credit"

#: A method that says "refund".
REFUND_TOKENS = ("refund",)

#: The payment methods Meta's export uses.
PAYMENT_TOKENS = (
    "paypal",
    "visa",
    "mastercard",
    "master card",
    "american express",
    "amex",
    "card",
    "bank",
    "debit",
    "sepa",
    "apple pay",
    "google pay",
    "klarna",
    "ideal",
    "unionpay",
    "discover",
    "jcb",
)


# ─── THE ORDERED RULE TABLE ───────────────────────────────────────────────────

#: The rules, in the order they decide. Each names the kind it produces and
#: the evidence it needs. This table is the SPEC: `classify_record_kind`
#: implements it rule by rule, and the unit test walks a matrix of methods
#: through the implementation and checks the answer against this table, so the
#: table and the code cannot drift apart.
RECORD_KIND_RULES = (
    {"id": "reversal_by_name", "kind": IMPORT_KIND_REVERSAL, "evidence": "method name"},
    {"id": "credit_by_name", "kind": IMPORT_KIND_CREDIT, "evidence": "method name"},
    {"id": "credit_by_word", "kind": IMPORT_KIND_CREDIT, "evidence": "method name, and not a card name"},
    {"id": "refund_by_name", "kind": IMPORT_KIND_REFUND, "evidence": "method name"},
    {"id": "refund_by_sign", "kind": IMPORT_KIND_REFUND, "evidence": "a negative amount"},
    {"id": "payment_by_name", "kind": IMPORT_KIND_PAYMENT, "evidence": "method name"},
    {"id": "unclassified", "kind": IMPORT_KIND_UNCLASSIFIED, "evidence": "the file did not say"},
)

#: The rule that names a kind when the method names a token.
_KIND_BY_NAME_RULE = {
    "reversal_by_name": IMPORT_KIND_REVERSAL,
    "credit_by_name": IMPORT_KIND_CREDIT,
    "credit_by_word": IMPORT_KIND_CREDIT,
    "refund_by_name": IMPORT_KIND_REFUND,
    "refund_by_sign": IMPORT_KIND_REFUND,
    "payment_by_name": IMPORT_KIND_PAYMENT,
}


def _normalise_method(method: str) -> str:
    """Fold a payment method to the comparable form: letters and digits only."""
    text = "".join(character if character.isalnum() else " " for character in str(method).casefold())
    return " ".join(text.split())


def _matches_tokens(normalised: str, tokens: tuple[str, ...]) -> bool:
    """True when any token appears in the normalised method."""
    return any(token in normalised for token in tokens)


def classify_record_kind(method: str, amount: Decimal | None = None) -> str:
    """Name the kind of one Meta billing row.

    The method is the file's own statement about the record. The amount is
    only consulted when the method says nothing at all, because a negative
    amount with no named method is still money coming back.

    Returns one of `IMPORT_RECORD_KINDS`.
    """
    normalised = _normalise_method(method)
    if not normalised:
        return IMPORT_KIND_UNCLASSIFIED

    # RULE 1 — reversal_by_name. A reversal undoes a prior document, so it
    # wins over every other token: "Ad Credit Reversal" and "Credit Card
    # Reversal" both say "reversal" first.
    if _matches_tokens(normalised, REVERSAL_TOKENS):
        return _KIND_BY_NAME_RULE["reversal_by_name"]

    # RULE 2 — credit_by_name. Meta's promotional credit.
    if _matches_tokens(normalised, CREDIT_TOKENS):
        return _KIND_BY_NAME_RULE["credit_by_name"]

    # RULE 3 — credit_by_word. The bare word, but never a card name.
    if CREDIT_WORD in normalised.split() and not _matches_tokens(normalised, PAYMENT_TOKENS):
        return _KIND_BY_NAME_RULE["credit_by_word"]

    # RULE 4 — refund_by_name.
    if _matches_tokens(normalised, REFUND_TOKENS):
        return _KIND_BY_NAME_RULE["refund_by_name"]

    # RULE 5 — refund_by_sign. The export's sign convention for a refund is
    # not fixed, so the sign is the evidence when the name is not.
    if amount is not None and amount < 0:
        return _KIND_BY_NAME_RULE["refund_by_sign"]

    # RULE 6 — payment_by_name.
    if _matches_tokens(normalised, PAYMENT_TOKENS):
        return _KIND_BY_NAME_RULE["payment_by_name"]

    # RULE 7 — unclassified. The file did not say.
    return IMPORT_KIND_UNCLASSIFIED


# ─── THE IMPORT ROW STATUSES ──────────────────────────────────────────────────

#: What an import may do with a row that is already stored.
IMPORT_ROW_NEW = "new"
IMPORT_ROW_DUPLICATE = "duplicate"
IMPORT_ROW_CONFLICT = "conflict"

IMPORT_ROW_STATUSES = (IMPORT_ROW_NEW, IMPORT_ROW_DUPLICATE, IMPORT_ROW_CONFLICT)


# ─── THE VALIDATION VOCABULARY ────────────────────────────────────────────────

#: Why ONE ROW could not be read. A closed vocabulary, so the application can
#: translate every code and the parity guard can hold both sides to it.
ROW_VALIDATION_CODES = (
    "INVALID_DATE",
    "DATE_OUTSIDE_PERIOD",
    "INVALID_AMOUNT",
    "ZERO_AMOUNT",
    "INVALID_CURRENCY",
    "CURRENCY_MISMATCH",
    "MISSING_TRANSACTION_ID",
    "MISSING_PAYMENT_METHOD",
)

#: Why the FILE as a whole cannot be imported. These are the checks that
#: speak about the file, not about a row, so their codes never overlap the
#: row vocabulary above.
FILE_VALIDATION_CODES = (
    "ACCOUNT_MISMATCH",
    "MULTIPLE_PERIODS",
    "INVALID_PERIOD",
    "MISSING_TRANSACTION_ROWS",
    "MIXED_CURRENCIES",
    "SECTION_TOTAL_MISMATCH",
    "SECTION_TOTAL_MISSING",
    "SECTION_TOTAL_UNVERIFIED",
    "UNSUPPORTED_ROW",
    "INVALID_TOTAL",
    "TOTAL_CURRENCY_MISMATCH",
    "EMPTY_PAYMENT_METHOD_SECTION",
    "MISSING_PAYMENT_METHOD_SECTION",
)


# ─── THE SETTLEMENT FORMULA ───────────────────────────────────────────────────

def settlement_net(
    payments: Decimal | int | str = 0,
    refunds: Decimal | int | str = 0,
    credits: Decimal | int | str = 0,
    reversals: Decimal | int | str = 0,
    unclassified: Decimal | int | str = 0,
) -> Decimal:
    """The settlement position: what Meta billed, net of what came back.

    Payments ADD. Refunds, credits and reversals SUBTRACT — they are all
    magnitudes of money moving back towards LaraTik. Unclassified keeps its
    own sign, because its direction is not known and guessing one would be
    inventing a settlement.
    """
    return (
        Decimal(str(payments))
        - Decimal(str(refunds))
        - Decimal(str(credits))
        - Decimal(str(reversals))
        + Decimal(str(unclassified))
    )


def kind_total_magnitude(kind: str, amount: Decimal) -> Decimal:
    """The magnitude a kind's total is stated in.

    A reducing kind's total is a MAGNITUDE, so the settlement formula can
    subtract it without caring which way the export carried the sign. An
    increasing kind keeps its sign.
    """
    if kind in SETTLEMENT_REDUCING_KINDS:
        return abs(amount)
    return amount
