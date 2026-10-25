"""Give existing cash boxes the safe, non-Main role (ADR-197)."""

import frappe


def execute():
    meta = frappe.get_meta("LaraTik Cash Box")
    if not meta.get_field("balance_role"):
        frappe.throw(
            "LaraTik Cash Box.balance_role is missing after model sync; "
            "refusing to mark the role migration complete."
        )

    invalid = frappe.db.sql(
        """
        SELECT name, balance_role
        FROM `tabLaraTik Cash Box`
        WHERE balance_role IS NOT NULL
          AND TRIM(balance_role) <> ''
          AND balance_role NOT IN ('Main', 'Custody')
        LIMIT 1
        """,
        as_dict=True,
    )
    if invalid:
        row = invalid[0]
        frappe.throw(
            f"Cash box {row.get('name')} has an unsupported balance_role; "
            "the role migration cannot guess its meaning."
        )

    # COUNTED BEFORE THE WRITE, so the log says how many rows this patch
    # actually filled rather than how many rows happen to be Custody.
    blanks = frappe.db.sql(
        """
        SELECT COUNT(*) AS blanks
        FROM `tabLaraTik Cash Box`
        WHERE balance_role IS NULL OR TRIM(balance_role) = ''
        """,
        as_dict=True,
    )
    filled = blanks[0].get("blanks") if blanks else 0

    frappe.db.sql(
        """
        UPDATE `tabLaraTik Cash Box`
        SET balance_role = %s
        WHERE balance_role IS NULL OR TRIM(balance_role) = ''
        """,
        ("Custody",),
    )

    # NOT SILENT. A migration that changes rows and says nothing cannot be
    # audited, and this one changes what the app resolves as Main. It also
    # never promotes a row: every cash box ends this patch as Custody unless
    # an Admin declares it Main through the settings page.
    frappe.logger().info(
        "laratik_cashbox: set balance_role to Custody on %s cash box(es); "
        "no box was promoted to Main — declare it in the app's Main cash box "
        "settings for each company.",
        filled,
    )
