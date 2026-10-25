"""The ONE rule that says which cash box is Main (ADR-197, ADR-198).

A company has at most one declared Main cash box. The company a cash box
belongs to comes from the Account it links to — never from a second company
field on the DocType, and never from a deployment-level name (ADR-198 removed
that fallback).

Both writers of the role use this module: the settings API
(``api.set_main_cash_box``) and the Desk save path
(``LaraTik Cash Box.validate``). One rule, one place.

─── WHY THE UNIQUENESS READ IS A LOCKING READ ────────────────────────────────

The Company row lock alone does NOT make the uniqueness check safe, and the
reason is worth writing down, because it is the kind of thing that looks
correct.

MariaDB runs REPEATABLE READ by default, and Frappe treats snapshot isolation
as the normal mode — ``frappe/database/mariadb/database.py`` treats
``ER_CHECKREAD`` as a deadlock, which is what a snapshot conflict looks like.
Under REPEATABLE READ a plain SELECT reads the snapshot taken by this
transaction's FIRST consistent read. In the settings API that first read
happens BEFORE the Company lock (the Company document is fetched to check the
caller's permission), so when a second declaration for the same company is
waiting for that lock, the plain uniqueness read cannot see the Main box the
first declaration committed. Both declarations pass the check. The company
ends up with two Main boxes, and every screen that resolves the role then has
to pick between them.

A locking read (``FOR UPDATE``) is a CURRENT read: it reads the latest
committed rows rather than the snapshot, and it holds them until this
transaction commits or rolls back. So the uniqueness CHECK is what makes the
invariant hold, and the Company lock is what stops two declarations from
interleaving in the first place.

This module is Frappe-aware rather than pure, unlike ``rollup.py`` and
``currency_policy.py``: the rule is not "a list of rows in, a verdict out", it
is "a CURRENT read, then a verdict". The part that decides — which company a
declared row belongs to — is still one small function, and it is the part the
tests drive.
"""

import frappe

MAIN_BALANCE_ROLE = "Main"
CUSTODY_BALANCE_ROLE = "Custody"


def declared_main_cash_boxes():
    """Every cash box declared Main, read as a CURRENT read.

    ``FOR UPDATE`` is what makes this a locking read, so it sees rows another
    transaction committed after this transaction's snapshot was taken, and it
    holds them until this one ends.

    It locks every row it scans, not only the matching ones — ``balance_role``
    carries no index — so a declaration briefly holds the cash-box table. That
    is deliberate: it is an Admin action that happens rarely, and it is the
    thing that makes "at most one Main per company" true rather than hoped
    for.
    """
    return frappe.db.sql(
        """
        SELECT name, cash_account
        FROM `tabLaraTik Cash Box`
        WHERE balance_role = %s
        FOR UPDATE
        """,
        (MAIN_BALANCE_ROLE,),
        as_dict=True,
    )


def main_cash_box_company(row):
    """The company that owns a declared Main cash box.

    ``None`` means the scope could NOT be established — a missing or unreadable
    Account, not "another company". A caller that cannot establish the scope
    must fail closed rather than treat the row as somebody else's.
    """
    account_name = (row.get("cash_account") or "").strip()
    if not account_name:
        return None
    company = frappe.db.get_value("Account", account_name, "company")
    return (company or "").strip() or None
