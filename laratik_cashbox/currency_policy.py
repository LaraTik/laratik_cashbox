"""Which rows may be summed into a cash box's balance.

─── WHY THIS FILE EXISTS, AND WHY IT HAS NO `import frappe` ───────────────────

The currency invariant lived in ONE process: the application's write path,
which rejects any currency other than the instance's own and says so loudly.
The OTHER process — the Frappe rollup — summed `amount` regardless. A row
inserted from Desk with `currency = "EUR"` landed in a USD balance with no
conversion and no warning.

One process enforcing an invariant is not an invariant. It is a convention
that happens to hold on the path somebody remembered.

So the rule moves here, to a module that is pure arithmetic over three
strings, and the rollup calls it. The rule is stated once, tested without a
site, and survives the ledger being rewritten underneath it.

This module is pure for the same reason `rollup.py` is: the Frappe app had
nine Python files and zero `test_*.py`, because everything in it needed a
live site. A module that needs no site can be tested.

─── WHY THE BOX'S CURRENCY IS NOT THE COMPANY'S ───────────────────────────────

`LaraTik Cash Box` has no currency column of its own. Its currency is the
currency of the GL account it maps to — `cash_account` → `Account` — because
that is where the money actually is. A company may hold cash in more than one
currency, and the account is the thing that says which.

─── THE FIELD IS `account_currency`, NOT `currency` ───────────────────────────

This is worth writing down, because the audit that raised F7 said the box's
currency is implied by `cash_account` → `Account.currency`, and **the ERPNext
`Account` doctype has no field called `currency`.** Its currency field is
**`account_currency`** (a Link to `Currency`), and the pinned v16 schema in
`cashbox-app/fixtures/erpnext-v16/account.json` is the evidence: its only
currency-bearing field is `account_currency`.

A fix written from that sentence would read a field that does not exist, and
would silently compare against nothing.

`Company` has `default_currency` (required) and `reporting_currency`. The
company's default is the FALLBACK for an account that names no currency of
its own — not the authority. The account is the authority, because the
account is where the money is.
"""

from __future__ import annotations

#: The ERPNext `Account` field that names the currency the account holds.
#: NOT `currency` — the doctype has no field by that name.
ACCOUNT_CURRENCY_FIELD = "account_currency"

#: The ERPNext `Company` field that names the company's currency. The FALLBACK,
#: used only when the account itself names no currency.
COMPANY_CURRENCY_FIELD = "default_currency"


def box_currency(
    account_currency: str | None,
    company_currency: str | None = None,
) -> str | None:
    """The currency a cash box's amounts are in, or `None` if it is unknown.

    :param account_currency: the `Account.account_currency` of the box's
        `cash_account`. This is the authority: the money is in the ACCOUNT.
    :param company_currency: the `Company.default_currency` of that account's
        company. Used only when the account names no currency of its own.
    :returns: the currency, or `None` when neither names one.
    """
    account = (account_currency or "").strip()
    if account:
        return account
    company = (company_currency or "").strip()
    return company or None


def row_is_sumable(
    row_currency: str | None,
    box_currency_value: str | None,
) -> bool:
    """May a row in `row_currency` be summed into a box in `box_currency_value`?

    ─── THE RULE, AND ITS TWO EXCEPTIONS ───────────────────────────────────────

    A row may be summed when its currency is the box's currency. Two cases are
    NOT a mismatch, and both are deliberate:

    - **the box's currency is unknown** (`None`): there is nothing to compare
      against. The caller decides what to do with an unknown — the rollup
      treats it as "no check possible" rather than inventing a rule.
    - **the row names no currency**: it is unverified. When the box currency
      is known, the row is refused until its denomination is proven. This
      keeps legacy unknown-currency amounts out of complete totals.

    A row that names a DIFFERENT currency is refused, always. There is no
    conversion here and there is no exchange rate in this module: EUR is not
    added to USD because nobody said how, and a balance that quietly adds
    two currencies is worse than a balance that refuses to be stated.

    ─── WHY "REFUSED" AND NOT "CONVERTED" ───────────────────────────────────────

    A conversion needs a rate, a rate needs a date, and a rate that is wrong
    is a wrong number that looks right. This module has no rate and no date
    and does not want them: refusing is the only answer that cannot be
    silently wrong.
    """
    box = (box_currency_value or "").strip()
    if not box:
        return True
    row = (row_currency or "").strip()
    if not row:
        return False
    return row == box
