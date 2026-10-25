"""`LaraTik Cash Box` — the cashbox itself, and the parent of both child tables.

─── THE BALANCE COLUMNS, AND WHO OWNS THEM NOW ────────────────────────────────

The balance fields on this doctype — `current_balance`, `total_received`,
`total_distributed`, `total_outstanding`, `total_owed`, `pending_count`,
`last_activity` and the four `pending_*` columns — are declared `read_only`
in the DocType JSON. They are declared that way because the LaraTik Cashbox
application must NEVER author a balance: the balance is a fact about the
ledger, and an application that can write one can also invent one. That
invariant is stated in `docs/02-PRODUCT-ARCHITECTURE.md` and it is enforced
in the application.

**This controller computes them.** That is the change. Until now they were
maintained by a Frappe Server Script on the instance — a script that was not
part of this repository, was not installed by `bench install-app`, and was
therefore missing on every fresh install. An instance without it showed
`$0.00` balances while the real rows sat in `LaraTik Cash Box.entries`, and
nothing in the schema or the deploy procedure said so.

The computation lives in `rollup.py`, which has no Frappe import and is
tested by `laratik_cashbox/tests/test_rollup.py` — the first test suite this
app has ever had.

─── WHY `read_only` IS NOT ENOUGH, AND WHY IT IS STILL RIGHT ──────────────────

A `read_only` flag is a DESK-level guard. It stops a human editing the field
in the ERPNext form. It does NOT stop the REST API from writing it, and it
does not stop `frappe.db.set_value`. The schema marks the field as derived;
it does not enforce that.

The enforcement is the APPLICATION's: it never writes a balance, it only
reads one. This controller is the other half of that arrangement — it is the
one place a balance is written, and it writes it from the ledger.

─── WHY THERE IS NO `company` FIELD ─────────────────────────────────────────

A cashbox belongs to a company through its GL account:
`LaraTik Cash Box.cash_account` → `Account.company`. A `company` column here would
be a second, independently-writable answer to the same question, and the
app does not read one — so a wrong value in it would be invisible to the
app and would still be sitting in the database, inviting the next person to
filter on it. The GL-account route is the one the app actually uses
(ADR-098).

─── WHY THERE IS A `balance_rule` FIELD ──────────────────────────────────────

`current_balance` has TWO definitions, and which one applies depends on
whether the box is the company's main cash box:

  • main box — `total_received` − `total_distributed` (cash on hand)
  • other boxes — `total_outstanding` (issued + loan given − loan repaid)

That used to be private knowledge inside the instance's Server Script, which
is why the schema could not express it and why a fresh install had no way to
be right. The rule is now a FIELD, so the schema states it and the rollup
applies it. The default is `Outstanding`, because every box except the main
one uses it, and it is therefore the only default that cannot change a figure
somebody already has.

`balance_rule` is a different question from `box_type`. `box_type` says
WHOSE box this is (Employee / Client / Vendor). `balance_rule` says how the
balance is DEFINED. They are not the same thing, and an earlier version of
the application's type comment claimed `box_type` was the balance-formula
discriminator; it is not, and that comment was wrong.
"""

import frappe
from frappe.model.document import Document

from laratik_cashbox.currency_policy import box_currency
from laratik_cashbox.idempotency import duplicate_idempotency_keys
from laratik_cashbox.main_role import declared_main_cash_boxes, main_cash_box_company
from laratik_cashbox.reversal import (
    duplicate_reversal_targets,
    orphan_reversal_legs,
    unjustified_reversed_rows,
)
from .rollup import (
    DEFAULT_BALANCE_RULE,
    RULE_OUTSTANDING,
    RULE_RECEIVED_MINUS_DISTRIBUTED,
    compute_rollup,
)

#: The derived fields this controller writes, and nothing else.
DERIVED_FIELDS = (
    "current_balance",
    "total_cash",
    "total_received",
    "total_distributed",
    "total_outstanding",
    "last_activity",
)


class LaraTikCashBox(Document):
    """The cashbox. Thin on purpose: the schema is the contract.

    `autoname` is `hash`, which is what the app expects: the application
    treats a box's name as an opaque identifier and never parses it.
    """

    def validate(self):
        """A cash box may not carry the same idempotency key twice.

        ─── WHY THIS IS HERE AND NOT IN THE APPLICATION ───────────────────────

        ADR-113's registry is PROCESS-LOCAL: a restart, or a second instance,
        forgets every key. So a retry after a lost response — the case
        ADR-112 made honest by answering `ERP_OUTCOME_UNKNOWN` — appends a
        SECOND money row to the box's child table, and nothing notices.

        The durable half of that story is the key ON THE ROW, and this is the
        check that makes it durable: it refuses a box that would carry one
        key twice.

        The application queries the box's existing rows before appending, so
        this is the BACKSTOP. It is what stops two writers racing, and what
        stops a client that mints the same key twice.
        """
        # Employee is the identity authority. Keep the legacy display field in
        # sync for existing readers, but never accept a conflicting name when
        # the standard Employee link is present.
        employee = getattr(self, "employee", None)
        if employee:
            employee_name = frappe.db.get_value("Employee", employee, "employee_name")
            if not employee_name:
                frappe.throw(f"Employee {employee} does not exist or has no employee name.")
            self.employee_name = employee_name
        self._assert_main_balance_role_is_unique()
        self._assert_unique_idempotency_keys()
        self._assert_reversals_are_complete()

    def _assert_main_balance_role_is_unique(self) -> None:
        """A company has at most one declared Main cash box (ADR-197).

        Company ownership comes from the linked Account, not a second company
        field on this DocType. The company row is the serialization lock shared
        with the settings API, and the uniqueness read itself is a CURRENT read
        — so two concurrent saves cannot both pass the uniqueness check for
        different boxes in the same company. The lock alone would not do it:
        see `laratik_cashbox/main_role.py` for why a plain read is not enough.
        """
        if self.get("balance_role") != "Main":
            return

        account_name = (self.get("cash_account") or "").strip()
        if not account_name:
            frappe.throw("A Main cash box must link to a cash Account.")

        account = frappe.db.get_value("Account", account_name, ["company"], as_dict=True)
        company = (account or {}).get("company") if isinstance(account, dict) else None
        company = (company or "").strip()
        if not company:
            frappe.throw("A Main cash box must link to an Account with a readable Company.")
        if self.get("balance_rule") != RULE_RECEIVED_MINUS_DISTRIBUTED:
            frappe.throw(
                "A Main cash box must use the 'Received minus distributed' balance rule."
            )

        frappe.db.sql(
            "SELECT name FROM `tabCompany` WHERE name = %s FOR UPDATE",
            (company,),
        )
        # THE UNIQUENESS READ IS A CURRENT READ, NOT A PLAIN ONE. See
        # `laratik_cashbox/main_role.py`: under REPEATABLE READ a plain read
        # cannot see the Main box that a concurrent declaration committed
        # while this transaction was waiting for the Company lock, so both
        # would pass and the company would end up with two Main boxes.
        for row in declared_main_cash_boxes():
            if row.get("name") == self.name:
                continue
            other_company = main_cash_box_company(row)
            if not other_company:
                frappe.throw(
                    "Another Main cash box has no readable Account company; "
                    "the uniqueness of this company's Main box cannot be established."
                )
            if other_company == company:
                frappe.throw(
                    f"Company {company} already has Main cash box {row.get('name')}. "
                    "Set that box to Custody before declaring another Main box."
                )

    def _assert_unique_idempotency_keys(self) -> None:
        """Refuse a box whose entries carry the same idempotency key twice.

        The rule itself is the pure `duplicate_idempotency_keys` — tested
        without a site, in `laratik_cashbox/tests/test_idempotency.py`. This
        is only the refusal.
        """
        duplicates = duplicate_idempotency_keys(self.get("entries") or [])
        if not duplicates:
            return
        key, first = duplicates[0]
        frappe.throw(
            f"The idempotency key {key} is already used on this cash box, by the entry "
            f"{first}. A cash box may not carry the same idempotency key twice: it is how "
            "one movement becomes two money rows."
        )

    def _assert_reversals_are_complete(self) -> None:
        """Refuse a box whose reversals do not add up (ADR-177).

        A reversal is a PAIR: the original marked `Reversed`, and the leg
        that undoes it. The application writes both in one request, so this
        is the BACKSTOP — it catches a client that wrote only one of them, a
        migration that marked rows without posting their legs, and a
        hand-edited row.

        Three ways to be wrong, and each is a movement that has been taken
        out of the box's cash measure with nothing to take it back out of:

        * two legs against one original — the correction is applied twice.
        * a leg naming an original this box does not carry — a correction of
          a movement nobody can see.
        * a row marked `Reversed` with no leg against it — the mark without
          the justification.
        """
        entries = self.get("entries") or []

        duplicates = duplicate_reversal_targets(entries)
        if duplicates:
            target, first = duplicates[0]
            frappe.throw(
                f"The entry {target} is already reversed by {first}. A cash box may not carry "
                "two reversals of the same movement: the correction would be applied twice, "
                "and the box's cash would be wrong by that amount."
            )

        orphans = orphan_reversal_legs(entries)
        if orphans:
            frappe.throw(
                f"The entry {orphans[0]} is a reversal of a movement this cash box does not "
                "carry. A reversal must name the movement it undoes, or the box's cash moves "
                "with nothing to take it back out of."
            )

        unjustified = unjustified_reversed_rows(entries)
        if unjustified:
            frappe.throw(
                f"The entry {unjustified[0]} is marked Reversed but nothing reverses it. A "
                "reversal is a pair — the row and the leg that undoes it — and the two are "
                "written together."
            )

    def on_update(self):
        """Recompute the derived columns after the box is written.

        `on_update` rather than `validate`, because the rollup reads the
        CHILD ROWS — and a child row written in the same request is not
        necessarily in `self.entries` until the parent is saved.
        """
        self.apply_rollup()

    def apply_rollup(self):
        """Write the derived columns from the child rows.

        `db_set` rather than `self.save()`: a save would re-run `on_update`,
        which would recompute the rollup, which would save again. `db_set`
        writes the row without re-entering the document lifecycle, which is
        exactly what a derived field wants.
        """
        values = self.compute_rollup()
        if values is None:
            return
        self.db_set(values, update_modified=False)

    def _box_currency(self):
        """The currency this box's amounts are in, or `None` when unknown.

        ─── THE ACCOUNT IS THE AUTHORITY ────────────────────────────────────────

        The money is in the ACCOUNT, so the account's currency is the box's
        currency. ERPNext's `Account` doctype has **no field called
        `currency`** — the field is `account_currency`, a Link to `Currency`.
        A resolver written against the word "currency" reads a field that does
        not exist and compares against nothing, silently.

        The COMPANY is the FALLBACK, used only when the account names no
        currency of its own: `Company.default_currency`.

        `None` means unknown, and unknown is NOT a mismatch — the rollup treats
        it as "no check possible" rather than inventing a rule.
        """
        account = self.get("cash_account")
        if not account:
            return None
        row = frappe.db.get_value(
            "Account", account, ["account_currency", "company"], as_dict=True
        )
        if not row:
            return None
        company_currency = None
        if row.get("company"):
            company_currency = frappe.db.get_value(
                "Company", row.get("company"), "default_currency"
            )
        return box_currency(row.get("account_currency"), company_currency)

    def compute_rollup(self):
        """The derived figures, or `None` when they cannot be computed.

        `None` means "do not write anything" — which is different from
        "write zero". A box whose child rows could not be read gets its
        columns left alone, and the readiness screen says so, rather than
        being overwritten with a confident `$0.00`.
        """
        rule = self.get("balance_rule") or DEFAULT_BALANCE_RULE
        if rule not in (RULE_RECEIVED_MINUS_DISTRIBUTED, RULE_OUTSTANDING):
            # An unknown rule does not get to pick the answer. The
            # conservative rule is the one that cannot make a balance look
            # larger than it is.
            rule = DEFAULT_BALANCE_RULE

        try:
            legacy_rows = self.get("entries") or []
            movement_fields = ["name", "movement_date", "source_cash_box", "destination_cash_box",
                               "direction", "amount", "currency", "category", "variance_type",
                               "legacy_entry_name"]
            mirrored_names = self._mirrored_legacy_entry_names()

            def projected_rows():
                for row in legacy_rows:
                    entry_name = row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
                    if entry_name not in mirrored_names:
                        yield row
                for movement in self._submitted_movement_pages(
                    {"docstatus": 1, "source_cash_box": self.name}, movement_fields
                ):
                    yield {
                        "name": movement.name, "entry_date": movement.movement_date,
                        "direction": movement.direction, "amount": movement.amount,
                        "currency": movement.currency, "category": movement.category,
                        "status": "Completed", "approval_status": "Approved",
                        "variance_type": movement.variance_type,
                        "other_box": movement.destination_cash_box,
                        "standalone_docstatus": 1,
                    }
                for movement in self._submitted_movement_pages(
                    {"docstatus": 1, "destination_cash_box": self.name}, movement_fields
                ):
                    yield {
                        "name": movement.name, "entry_date": movement.movement_date,
                        "direction": "In", "amount": movement.amount,
                        "currency": movement.currency, "category": movement.category,
                        "status": "Completed", "approval_status": "Approved",
                        "variance_type": movement.variance_type,
                        "other_box": movement.source_cash_box,
                        "standalone_docstatus": 1,
                    }
            computed = compute_rollup(
                projected_rows(),
                self.get("pending_items") or [],
                rule=rule,
                box_currency_value=self._box_currency(),
            )
        except Exception:
            # A rollup that cannot be computed must not half-write itself.
            # The columns keep their previous values, and the application
            # reads them as they were.
            self.logger().error(
                "LaraTik Cash Box %s: the balance rollup could not be computed; the derived "
                "columns were left unchanged.",
                self.name,
                exc_info=True,
            )
            return None

        if not computed.get("readable", True):
            self.logger().warning(
                "LaraTik Cash Box %s: %s row(s) had an unreadable amount. Derived columns "
                "were cleared because a partial total is not a complete balance.",
                self.name,
                computed.get("unreadable_count", 0),
            )
            return {field: None for field in DERIVED_FIELDS}

        return {field: computed.get(field) for field in DERIVED_FIELDS}

    def _mirrored_legacy_entry_names(self):
        names = set()
        last_name = None
        while True:
            page = frappe.get_all(
                "LaraTik Cash Movement",
                filters={
                    "legacy_entry_name": ["is", "set"],
                    "docstatus": 1,
                    "migration_state": ["in", ["Native", "Verified"]],
                    **({"name": [">", last_name]} if last_name else {}),
                },
                or_filters=[
                    {"source_cash_box": self.name},
                    {"destination_cash_box": self.name},
                ],
                fields=["name", "legacy_entry_name"], order_by="name asc",
                limit_page_length=500,
            )
            names.update(row.legacy_entry_name for row in page if row.legacy_entry_name)
            if len(page) < 500:
                return names
            last_name = page[-1].name

    def _submitted_movement_pages(self, filters, fields):
        last_name = None
        while True:
            page_filters = dict(filters)
            if last_name:
                page_filters["name"] = [">", last_name]
            page = frappe.get_all(
                "LaraTik Cash Movement", filters=page_filters, fields=fields,
                order_by="name asc", limit_page_length=500,
            )
            yield from page
            if len(page) < 500:
                return
            last_name = page[-1].name
