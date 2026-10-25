"""Permission-checked, bounded read APIs for the LaraTik cashbox app."""

import json

import frappe

from laratik_cashbox.setup_state import ACCOUNT_MAPPING_ROOT_TYPES, normalize_setup_state
from laratik_cashbox.laratik_cashbox.doctype.laratik_cash_box.rollup import (
    RULE_RECEIVED_MINUS_DISTRIBUTED,
)
from laratik_cashbox.main_role import declared_main_cash_boxes, main_cash_box_company


MAX_CASHBOX_ROLLUP_BATCH = 200
ROLLUP_FIELDS = (
    "current_balance",
    "total_cash",
    "total_received",
    "total_distributed",
    "total_outstanding",
    "last_activity",
)


@frappe.whitelist()
def get_cashbox_rollups(box_names):
    """Return authoritative operational summaries for a bounded set of boxes.

    The computation delegates to ``LaraTik Cash Box.compute_rollup`` so the
    standalone and legacy paths share the same currency, status, transfer,
    reversal, and pending-roster rules. This avoids sending a company's entire
    movement history to Next.js for list summaries.

    This is a read API: each cashbox, its linked Account, and derived Company
    must be readable to the authenticated Frappe principal. Unknown or
    unreadable boxes fail closed instead of returning a partial result set.
    """
    names = _parse_box_names(box_names)
    result = []
    for name in names:
        box = frappe.get_doc("LaraTik Cash Box", name)
        box.check_permission("read")
        account_name = (box.get("cash_account") or "").strip()
        if not account_name:
            frappe.throw(f"Cash box {name} has no linked cash Account.")

        account = frappe.get_doc("Account", account_name)
        account.check_permission("read")
        company = (account.get("company") or "").strip()
        if not company:
            frappe.throw(f"Cash box {name} Account has no Company.")
        frappe.get_doc("Company", company).check_permission("read")

        computed = box.compute_rollup()
        readable = computed is not None and computed.get("readable", True)
        values = computed or {}
        result.append({
            "name": name,
            "company": company,
            "currency": box._box_currency(),
            "readable": bool(readable),
            "unreadable_count": values.get("unreadable_count", 0),
            **{
                field: values.get(field) if readable else None
                for field in ROLLUP_FIELDS
            },
        })
    return result


@frappe.whitelist(methods=["POST"])
def set_main_cash_box(company, cash_box):
    """Atomically declare one cash box as Main for one company (ADR-197).

    The application sends a company and a current document name. The method
    verifies ownership through ``cash_account -> Account.company`` and performs
    the role switch in the POST transaction. The Company row lock serializes
    concurrent declarations, and the uniqueness check is a CURRENT read, so
    the declaration that is queued behind the lock sees the Main box the first
    one committed. An exception rolls the whole request back.
    """
    _require_setup_admin()
    company_name = _crm_text(company, "Company", 140)
    cash_box_name = _crm_text(cash_box, "Cash box", 140)
    if not company_name or not cash_box_name:
        frappe.throw("Choose a company and a cash box.")

    company_doc = frappe.get_doc("Company", company_name)
    company_doc.check_permission("read")

    # Lock the company before reading or changing any role so a second caller
    # for this company waits until the first POST commits or rolls back.
    frappe.db.sql(
        "SELECT name FROM `tabCompany` WHERE name = %s FOR UPDATE",
        (company_name,),
    )
    frappe.db.sql(
        "SELECT name FROM `tabLaraTik Cash Box` WHERE name = %s FOR UPDATE",
        (cash_box_name,),
    )

    selected = frappe.get_doc("LaraTik Cash Box", cash_box_name)
    selected.check_permission("write")
    account_name = (selected.get("cash_account") or "").strip()
    if not account_name:
        frappe.throw("The selected cash box has no linked cash Account.")
    account = frappe.get_doc("Account", account_name)
    account.check_permission("read")
    owner_company = (account.get("company") or "").strip()
    if owner_company != company_name:
        frappe.throw("The selected cash box does not belong to this company.")
    if selected.get("balance_rule") != RULE_RECEIVED_MINUS_DISTRIBUTED:
        frappe.throw(
            "Choose a cash box whose balance rule is 'Received minus distributed'."
        )

    # THE UNIQUENESS READ IS A CURRENT READ, NOT A PLAIN ONE. See
    # `laratik_cashbox/main_role.py`: under REPEATABLE READ a plain read
    # cannot see the Main box that the declaration we are queued behind just
    # committed, so both declarations would pass and the company would end up
    # with two Main boxes.
    for row in declared_main_cash_boxes():
        if row.get("name") == cash_box_name:
            continue
        other_company = main_cash_box_company(row)
        if not other_company:
            frappe.throw(
                "An existing Main cash box has no readable Account company; "
                "the company scope cannot be established."
            )
        if other_company != company_name:
            continue
        previous = frappe.get_doc("LaraTik Cash Box", row["name"])
        previous.check_permission("write")
        previous.balance_role = "Custody"
        previous.save()

    if selected.get("balance_role") != "Main":
        selected.balance_role = "Main"
        selected.save()
    return {"company": company_name, "cash_box": cash_box_name}


@frappe.whitelist(methods=["GET"])
def get_setup_state():
    """Read the resumable, non-financial setup draft for System Managers."""
    _require_setup_admin()
    doc = frappe.get_single("LaraTik Setup State")
    state = normalize_setup_state(doc.setup_json or "{}")
    return {"status": doc.status or "Not Started", "state": state}


@frappe.whitelist(methods=["GET"])
def get_setup_accounts(company):
    """List leaf accounts for one readable Company during initial setup.

    ERPNext does not grant the integration identity generic Account reads.
    This narrowly scoped setup endpoint requires System Manager, checks the
    selected Company itself, and returns only that Company's posting accounts.
    It does not expose balances, ledger entries, or accounts from other
    companies.
    """
    _require_setup_admin()
    company_name = str(company or "").strip()
    if not company_name or not frappe.db.exists("Company", company_name):
        frappe.throw("Choose an existing Company before mapping accounts.")

    company_doc = frappe.get_doc("Company", company_name)
    company_doc.check_permission("read")
    return frappe.get_all(
        "Account",
        filters={"company": company_name, "is_group": 0},
        fields=["name", "account_name", "account_type", "root_type", "account_currency"],
        order_by="name asc",
        limit_page_length=2000,
    )


_CRM_PARTY_CONFIG = {
    "Customer": {
        "name_field": "customer_name",
        "type_field": "customer_type",
        "group_doctype": "Customer Group",
        "group_field": "customer_group",
        "has_territory": True,
    },
    "Supplier": {
        "name_field": "supplier_name",
        "type_field": "supplier_type",
        "group_doctype": "Supplier Group",
        "group_field": "supplier_group",
        "has_territory": False,
    },
}


@frappe.whitelist(methods=["GET"])
def get_crm_setup_options(party_type):
    """Return limited leaf reference options for Admin-only party setup."""
    _require_setup_admin()
    config = _crm_party_config(party_type)
    group_doctype = config["group_doctype"]
    type_field = frappe.get_meta(party_type).get_field(config["type_field"])
    type_options = [
        option.strip()
        for option in str(type_field.options or "").splitlines()
        if option.strip()
    ]

    groups = frappe.get_all(
        group_doctype,
        filters={"is_group": 0},
        fields=["name"],
        order_by="name asc",
        limit_page_length=2000,
    )
    territories = []
    if config["has_territory"]:
        territories = frappe.get_all(
            "Territory",
            filters={"is_group": 0},
            fields=["name"],
            order_by="name asc",
            limit_page_length=2000,
        )
    currencies = frappe.get_all(
        "Currency",
        filters={"enabled": 1},
        fields=["name", "currency_name"],
        order_by="name asc",
        limit_page_length=500,
    )
    return {
        "party_type": party_type,
        "type_options": type_options,
        "groups": groups,
        "territories": territories,
        "currencies": currencies,
    }


@frappe.whitelist(methods=["POST"])
def create_crm_party(party_type, values):
    """Create a standard Customer or Supplier with a strict master-data allow-list.

    This endpoint is called only from the Admin-gated application. It creates
    the ERPNext party document and deliberately accepts no balance, bank,
    invoice, payment, tax amount, employee, or custody fields.
    """
    _require_setup_admin()
    config = _crm_party_config(party_type)
    if not isinstance(values, dict):
        frappe.throw("Party values must be an object.")

    allowed = {"name", "type", "group", "currency", "email"}
    if config["has_territory"]:
        allowed.add("territory")
    unknown = set(values) - allowed
    if unknown:
        frappe.throw(f"Unsupported party fields: {', '.join(sorted(unknown))}")

    display_name = _crm_text(values.get("name"), "Name", 140)
    party_type_value = _crm_text(values.get("type"), "Type", 80)
    group_name = _crm_text(values.get("group"), "Group", 140)
    territory_name = _crm_text(values.get("territory"), "Territory", 140)
    currency = _crm_text(values.get("currency"), "Currency", 10)
    email = _crm_text(values.get("email"), "Email", 140)

    if not display_name or not party_type_value or not group_name:
        frappe.throw("Name, type, and group are required.")
    if config["has_territory"] and not territory_name:
        frappe.throw("Territory is required for a Customer.")

    type_options = [
        option.strip()
        for option in str(frappe.get_meta(party_type).get_field(config["type_field"]).options or "").splitlines()
        if option.strip()
    ]
    if party_type_value not in type_options:
        frappe.throw("Choose a supported party type.")

    _require_leaf_reference(config["group_doctype"], group_name, "group")
    if config["has_territory"]:
        _require_leaf_reference("Territory", territory_name, "territory")
    if currency:
        if not frappe.db.exists("Currency", currency) or not frappe.db.get_value("Currency", currency, "enabled"):
            frappe.throw("Choose an enabled currency.")

    name_field = config["name_field"]
    type_fieldname = config["type_field"]
    group_field = config["group_field"]
    expected = {
        name_field: display_name,
        type_fieldname: party_type_value,
        group_field: group_name,
        "default_currency": currency,
        "email_id": email,
    }
    if config["has_territory"]:
        expected["territory"] = territory_name

    duplicate = frappe.get_all(
        party_type,
        filters={name_field: display_name},
        fields=["name", *expected.keys()],
        order_by="name asc",
        limit_page_length=2,
    )
    if duplicate:
        if len(duplicate) == 1 and all(
            str(duplicate[0].get(field) or "").strip() == str(value or "").strip()
            for field, value in expected.items()
        ):
            return {"name": duplicate[0]["name"], "already_existed": True}
        return {"error_code": "CRM_CONFLICT"}

    doc_values = {"doctype": party_type, **expected}
    doc = frappe.get_doc(doc_values).insert(ignore_permissions=True)
    return {"name": doc.name, "already_existed": False}


def _crm_party_config(party_type):
    config = _CRM_PARTY_CONFIG.get(str(party_type or "").strip())
    if not config:
        frappe.throw("Choose Customer or Supplier.")
    return config


def _crm_text(value, label, maximum):
    if value is None:
        return ""
    if not isinstance(value, str):
        frappe.throw(f"{label} must be text.")
    value = value.strip()
    if len(value) > maximum:
        frappe.throw(f"{label} is too long.")
    return value


def _require_leaf_reference(doctype, name, label):
    if not name or not frappe.db.exists(doctype, name):
        frappe.throw(f"Choose an existing {label}.")
    if frappe.db.get_value(doctype, name, "is_group"):
        frappe.throw(f"Choose a leaf {label}.")


@frappe.whitelist(methods=["POST"])
def save_setup_state(state):
    """Save only validated company and account-mapping configuration inputs."""
    _require_setup_admin()
    try:
        state = normalize_setup_state(state)
        _validate_setup_state(state)
    except ValueError as exc:
        frappe.throw(str(exc))

    doc = frappe.get_single("LaraTik Setup State")
    doc.setup_json = json.dumps(state, ensure_ascii=False, sort_keys=True)
    doc.status = "In Progress"
    doc.save()
    return {"status": doc.status or "Not Started", "state": state}


def _require_setup_admin():
    """The setup profile is editable only by the ERPNext System Manager role."""
    if frappe.session.user == "Administrator":
        return
    frappe.only_for("System Manager")


def _validate_setup_state(state):
    company = state["company"]
    company_name = company["company_name"]

    if company["country"] and not frappe.db.exists("Country", company["country"]):
        raise ValueError("The selected country does not exist in ERPNext.")
    if company["default_currency"] and not frappe.db.exists("Currency", company["default_currency"]):
        raise ValueError("The selected currency does not exist in ERPNext.")

    chart = company["chart_of_accounts"]
    if chart:
        if not company["country"]:
            raise ValueError("Choose a country before selecting a chart of accounts.")
        from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import (
            get_charts_for_country,
        )

        if chart not in get_charts_for_country(company["country"]):
            raise ValueError("The selected chart is not available for this country.")

    start = company["fiscal_year_start_date"]
    end = company["fiscal_year_end_date"]
    year_name = company["fiscal_year_name"]
    if any((start, end, year_name)) and not all((start, end, year_name)):
        raise ValueError("Enter the fiscal-year name and both dates, or leave them blank until confirmed.")
    if start and end and year_name:
        from frappe.utils import getdate

        if getdate(start) > getdate(end):
            raise ValueError("The fiscal-year start date must be on or before its end date.")

    for mapping_key, account_name in state["account_mappings"].items():
        if not account_name:
            continue
        if not company_name:
            raise ValueError("Enter the company name before mapping ledger accounts.")
        account = frappe.db.get_value(
            "Account",
            account_name,
            ["company", "is_group", "root_type"],
            as_dict=True,
        )
        if not account:
            raise ValueError(f"Mapped account {account_name} does not exist.")
        if account["company"] != company_name:
            raise ValueError("Each mapped account must belong to the selected company.")
        if account["is_group"]:
            raise ValueError("A group account cannot be used as a posting account mapping.")
        expected_root = ACCOUNT_MAPPING_ROOT_TYPES[mapping_key]
        if account["root_type"] != expected_root:
            raise ValueError(f"{mapping_key} must map to an {expected_root} account.")


def _parse_box_names(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            frappe.throw("Cash box names must be a JSON array.")
    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
        frappe.throw("Cash box names must be a JSON array of names.")
    names = list(dict.fromkeys(name.strip() for name in value if name.strip()))
    if len(names) > MAX_CASHBOX_ROLLUP_BATCH:
        frappe.throw(f"At most {MAX_CASHBOX_ROLLUP_BATCH} cash boxes may be requested at once.")
    return names
