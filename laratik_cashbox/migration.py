"""Additive, resumable mirror migration for legacy embedded cash movements.

Run through ``bench execute``. The default is census/dry-run; writes require
``commit=True`` explicitly. Source rows are never changed or deleted.
"""

from decimal import Decimal, InvalidOperation

import frappe


SOURCE_DOCTYPE = "LaraTik Cash Box Entry"
TARGET_DOCTYPE = "LaraTik Cash Movement"
FUNDING_SOURCES = {"Company Cash", "Personal Cash", "Loan", "Other"}
MOVEMENT_CATEGORIES = {"Service Payment", "Vendor Payment", "Salary", "Reimbursement", "Loan", "Cash", "Other"}
COMPLETED_STATUSES = {"Completed", "Settled", "Reversed"}
VARIANCE_TYPES = {"None", "Match", "Overpayment", "Underpayment"}
ACCOUNTING_VOUCHER_DOCTYPES = {
    "Journal Entry", "Payment Entry", "Sales Invoice", "Purchase Invoice",
    "Expense Claim", "Payroll Entry", "Employee Advance",
}


def _company_and_currency(cash_box):
    account_name = frappe.db.get_value("LaraTik Cash Box", cash_box, "cash_account")
    if not account_name:
        return None, None, None
    account = frappe.db.get_value(
        "Account", account_name, ["company", "account_currency"], as_dict=True
    )
    if not account or not account.company:
        return None, None, None
    if account.account_currency:
        return account.company, account.account_currency, "Account.account_currency"
    # ERPNext v16's Account.get_account_currency() explicitly resolves a blank
    # Account.account_currency to that Account's Company.default_currency. Keep
    # this as standard ERPNext resolution, and record the provenance so this
    # cannot be mistaken for an app-invented assumption.
    currency = frappe.db.get_value("Company", account.company, "default_currency")
    source = "ERPNext Account currency fallback: Company.default_currency" if currency else None
    return account.company, currency, source


def _is_valid_amount(value):
    if value is None or value == "":
        return False
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, TypeError, ValueError):
        return False


def _is_positive_amount(value):
    if not _is_valid_amount(value):
        return False
    return Decimal(str(value)) > 0


def _same_decimal(left, right):
    try:
        return Decimal(str(left or 0)) == Decimal(str(right or 0))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _target_precision(fieldname, currency):
    """Resolve the target Currency field precision through Frappe v16 itself."""
    from frappe.model.meta import get_field_precision

    field = frappe.get_meta(TARGET_DOCTYPE).get_field(fieldname)
    if not field:
        return None
    return get_field_precision(field, currency=currency or None)


def _fits_precision(value, precision):
    if not isinstance(precision, int) or precision < 0 or not _is_valid_amount(value):
        return False
    try:
        number = Decimal(str(value))
        return number == number.quantize(Decimal(1).scaleb(-precision))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _variance_issue(variance_type, variance_amount):
    normalized_type = None if variance_type in (None, "", "None") else variance_type
    if normalized_type not in (None, *VARIANCE_TYPES - {"None"}):
        return "legacy variance type is not supported by the standalone movement Select"
    try:
        amount = Decimal(str(variance_amount or 0))
    except (InvalidOperation, TypeError, ValueError):
        return "legacy variance amount is unreadable"
    if not amount.is_finite() or amount < 0:
        return "legacy variance amount is not finite and nonnegative"
    if amount and normalized_type not in ("Overpayment", "Underpayment"):
        return "legacy nonzero variance lacks an Overpayment or Underpayment type"
    if not amount and normalized_type in ("Overpayment", "Underpayment"):
        return "legacy variance type requires a nonzero variance amount"
    return None


def _validate_page(start, limit):
    if (not isinstance(start, int) or isinstance(start, bool) or start < 0
            or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000):
        frappe.throw("Migration page start must be nonnegative and page size must be between 1 and 5000.")


def census(start=0, limit=500):
    """Return one bounded, read-only census page and its disposition totals."""
    _validate_page(start, limit)
    filters = {"parenttype": "LaraTik Cash Box", "parentfield": "entries"}
    source_count = frappe.db.count(SOURCE_DOCTYPE, filters=filters)
    rows = frappe.get_all(
        SOURCE_DOCTYPE,
        filters=filters,
        fields=["name", "parent", "entry_date", "direction", "amount", "currency", "category",
                "money_flow", "funding_source", "status", "approval_status", "counterparty", "variance_type",
                "variance_amount", "purpose", "notes", "other_box", "reversal_of", "reference_doctype",
                "reference_name", "idempotency_key", "owner"],
        order_by="parent asc, idx asc",
        limit_start=start,
        limit_page_length=limit,
    )
    target_by_legacy_name = {}
    if rows:
        source_names = {row.name for row in rows}
        source_names.update(row.reversal_of for row in rows if row.reversal_of)
        target_rows = frappe.get_all(
            TARGET_DOCTYPE,
            filters={"legacy_entry_name": ["in", list(source_names)]},
            fields=["name", "legacy_entry_name", "movement_date", "amount", "currency", "company",
                    "source_cash_box", "destination_cash_box", "reversal_of", "docstatus",
                    "direction", "category", "money_flow", "variance_type", "variance_amount",
                    "accounting_voucher_type", "accounting_voucher", "workflow_state",
                    "migration_state", "funding_source", "owner"],
            limit_page_length=max(limit, len(source_names)),
        )
        target_by_legacy_name = {row.legacy_entry_name: row for row in target_rows}
    scope_by_box = {}
    precision_by_field_currency = {}
    report = {"source_doctype": SOURCE_DOCTYPE, "target_doctype": TARGET_DOCTYPE,
              "start": start, "limit": limit, "total_source_rows": source_count,
              "page_rows": len(rows), "already_mirrored": 0,
              "verified_candidates": 0, "unverified_candidates": 0,
              "unmappable": 0, "rows": []}
    for row in rows:
        inbound_transfer = bool(row.other_box and row.direction == "In")
        source_box = row.other_box if inbound_transfer else row.parent
        destination_box = row.parent if inbound_transfer else (row.other_box or None)
        if source_box not in scope_by_box:
            scope_by_box[source_box] = _company_and_currency(source_box)
        company, account_currency, account_currency_source = scope_by_box[source_box]
        voucher_type = getattr(row, "reference_doctype", None) or None
        voucher_name = getattr(row, "reference_name", None) or None
        voucher_reason = None
        if bool(voucher_type) != bool(voucher_name):
            voucher_reason = "legacy accounting voucher reference is incomplete"
        elif voucher_type:
            if voucher_type not in ACCOUNTING_VOUCHER_DOCTYPES:
                voucher_reason = "legacy voucher type is not an allowed standard accounting DocType"
            else:
                voucher = frappe.db.get_value(
                    voucher_type, voucher_name, ["docstatus", "company"], as_dict=True
                )
                if not voucher or int(voucher.docstatus or 0) != 1:
                    voucher_reason = "legacy accounting voucher is missing or not submitted"
                elif voucher.company and voucher.company != company:
                    voucher_reason = "legacy accounting voucher belongs to another company"
        resolved_currency = row.currency or account_currency
        precision_reasons = []
        for fieldname, value in (("amount", row.amount), ("variance_amount", row.variance_amount)):
            if value in (None, ""):
                continue
            key = (fieldname, resolved_currency or "")
            if key not in precision_by_field_currency:
                try:
                    precision_by_field_currency[key] = _target_precision(fieldname, resolved_currency)
                except Exception:
                    precision_by_field_currency[key] = None
            precision = precision_by_field_currency[key]
            if not _fits_precision(value, precision):
                precision_reasons.append(fieldname)
        variance_reason = _variance_issue(row.variance_type, row.variance_amount)
        destination_scope = None
        if destination_box:
            if destination_box not in scope_by_box:
                scope_by_box[destination_box] = _company_and_currency(destination_box)
            destination_scope = scope_by_box[destination_box]
        target_row = target_by_legacy_name.get(row.name)
        target = target_row.name if target_row else None
        if target:
            report["already_mirrored"] += 1
        reason = None
        if not company:
            reason = "source cash box has no verifiable company"
        elif row.category == "Invoice Issued":
            reason = "an issued invoice is not a physical cash movement"
        elif row.category not in MOVEMENT_CATEGORIES:
            reason = "legacy category is not supported by the standalone movement Select"
        elif not _is_positive_amount(row.amount) or not row.entry_date or row.direction not in ("In", "Out"):
            reason = "required historical date, positive amount, or direction is unreadable"
        elif row.status is None:
            reason = "legacy movement status is missing and cannot establish whether cash moved"
        elif row.status not in ("Pending", "Completed", "Settled", "Cancelled", "Reversed"):
            reason = "legacy movement status is unknown and cannot be mapped safely"
        elif row.approval_status not in (None, "Not Required", "Pending", "Approved", "Rejected"):
            reason = "legacy approval status is unknown and cannot be mapped safely"
        elif row.funding_source not in (None, *FUNDING_SOURCES):
            reason = "legacy funding source is unknown and cannot be represented by the target Select"
        elif voucher_reason:
            reason = voucher_reason
        elif variance_reason:
            reason = variance_reason
        elif precision_reasons:
            reason = "legacy " + " and ".join(precision_reasons) + " exceeds effective ERPNext Currency field precision"
        elif row.status in COMPLETED_STATUSES and row.approval_status in ("Pending", "Rejected"):
            reason = "legacy completed movement conflicts with its pending or rejected approval"
        elif not (row.currency or account_currency):
            reason = "currency is not evidenced by the row or linked account"
        elif row.currency and account_currency and row.currency != account_currency:
            reason = "historical row currency conflicts with the linked cash account currency"
        elif destination_scope and (not destination_scope[0] or destination_scope[0] != company):
            reason = "legacy transfer cannot be represented as an intra-company custody event"
        elif destination_scope and account_currency and destination_scope[1] and account_currency != destination_scope[1]:
            reason = "legacy transfer crosses cash account currencies"
        if reason:
            report["unverified_candidates"] += 1
            state = "Unverified"
        else:
            report["verified_candidates"] += 1
            state = "Verified"
        unmappable = bool(
            not company
            or row.category == "Invoice Issued"
            or row.category not in MOVEMENT_CATEGORIES
            or bool(voucher_reason)
            or bool(variance_reason)
            or bool(precision_reasons)
            or not _is_positive_amount(row.amount)
            or not row.entry_date
            or row.direction not in ("In", "Out")
            or (destination_scope and (not destination_scope[0] or destination_scope[0] != company))
            or (destination_scope and account_currency and destination_scope[1] and account_currency != destination_scope[1])
        )
        if unmappable:
            report["unmappable"] += 1
        currency_source = (
            "LaraTik Cash Box Entry.currency"
            if row.currency
            else account_currency_source
        )
        original_target = target_by_legacy_name.get(row.reversal_of) if row.reversal_of else None
        reversal_target_is_submitted = bool(
            original_target and int(original_target.docstatus or 0) == 1
        )
        target_matches_source = None
        target_content_matches_source = None
        target_lifecycle_matches_source = None
        if target_row:
            try:
                same_amount = _is_valid_amount(target_row.amount) and Decimal(str(target_row.amount)) == Decimal(str(row.amount))
            except (InvalidOperation, TypeError, ValueError):
                same_amount = False
            if state == "Unverified":
                expected_workflow_state, expected_docstatus = "Draft", 0
            elif row.approval_status == "Rejected" or row.status == "Cancelled":
                expected_workflow_state, expected_docstatus = "Rejected", 0
            elif row.approval_status == "Pending" or row.status == "Pending":
                expected_workflow_state, expected_docstatus = "Pending Approval", 0
            else:
                expected_workflow_state, expected_docstatus = "Submitted", 1
            expected_reversal = original_target.name if reversal_target_is_submitted else None
            target_content_matches_source = bool(
                same_amount
                and str(target_row.movement_date or "") == str(row.entry_date or "")
                and target_row.currency == (row.currency or account_currency)
                and target_row.company == company
                and target_row.source_cash_box == source_box
                and target_row.destination_cash_box == destination_box
                and target_row.direction == ("Out" if row.other_box else row.direction)
                and target_row.category == (row.category or "Other")
                and target_row.money_flow == row.money_flow
                and target_row.funding_source == (row.funding_source if row.funding_source in FUNDING_SOURCES else None)
                and (target_row.variance_type or None) == (row.variance_type if row.variance_type not in (None, "None", "") else None)
                and _same_decimal(target_row.variance_amount, row.variance_amount)
                and target_row.accounting_voucher_type == voucher_type
                and target_row.accounting_voucher == voucher_name
                and target_row.migration_state == state
                and (not row.owner or target_row.owner == row.owner)
                and target_row.reversal_of == expected_reversal
            )
            target_lifecycle_matches_source = bool(
                int(target_row.docstatus or 0) == expected_docstatus
                and target_row.workflow_state == expected_workflow_state
            )
            target_matches_source = target_content_matches_source and target_lifecycle_matches_source
        report["rows"].append({"source": row.name, "cash_box": row.parent,
                               "source_cash_box": source_box,
                               "destination_cash_box": destination_box,
                               "mappable": not unmappable, "target": target,
                               "target_matches_source": target_matches_source,
                               "target_content_matches_source": target_content_matches_source,
                               "target_lifecycle_matches_source": target_lifecycle_matches_source,
                               "target_amount": str(target_row.amount) if target_row and _is_valid_amount(target_row.amount) else None,
                               "target_currency": target_row.currency if target_row else None,
                               "target_company": target_row.company if target_row else None,
                               "target_docstatus": int(target_row.docstatus or 0) if target_row else None,
                               "target_migration_state": target_row.migration_state if target_row else None,
                               "amount": str(row.amount) if _is_valid_amount(row.amount) else None,
                               "company": company, "currency": row.currency or account_currency,
                               "currency_source": currency_source,
                               "state": state, "reason": reason})
    return report


def reconciliation_report(page_size=500):
    """Return a read-only source-to-mirror reconciliation across all pages."""
    _validate_page(0, page_size)
    source_count = frappe.db.count(
        SOURCE_DOCTYPE,
        filters={"parenttype": "LaraTik Cash Box", "parentfield": "entries"},
    )
    source_by_scope = {}
    target_by_scope = {}
    totals = {
        "source_rows": source_count,
        "mirrored_rows": 0,
        "submitted_mirror_rows": 0,
        "verified_rows": 0,
        "unverified_rows": 0,
        "unmappable_rows": 0,
        "mirror_mismatches": 0,
        "content_mismatches": 0,
        "lifecycle_incomplete_rows": 0,
        "missing_mirrors": 0,
    }
    start = 0
    while start < source_count:
        page = census(start=start, limit=page_size)
        for row in page["rows"]:
            totals["mirrored_rows"] += int(bool(row["target"]))
            totals["missing_mirrors"] += int(not row["target"])
            totals["verified_rows"] += int(row["state"] == "Verified")
            totals["unverified_rows"] += int(row["state"] == "Unverified")
            totals["unmappable_rows"] += int(not row["mappable"])
            totals["mirror_mismatches"] += int(row["target_matches_source"] is False)
            if row["target"] and row["target_content_matches_source"] is False:
                totals["content_mismatches"] += 1
            if row["target"] and row["target_lifecycle_matches_source"] is False:
                totals["lifecycle_incomplete_rows"] += 1
            if row["state"] == "Verified":
                key = (row["company"], row["currency"])
                bucket = source_by_scope.setdefault(key, {"source_rows": 0, "source_amount": Decimal("0")})
                bucket["source_rows"] += 1
                bucket["source_amount"] += Decimal(str(row["amount"]))
            if row["target"]:
                if row["target_docstatus"] == 1:
                    totals["submitted_mirror_rows"] += 1
                    if row["target_migration_state"] == "Verified":
                        key = (row["target_company"], row["target_currency"])
                        bucket = target_by_scope.setdefault(key, {"target_rows": 0, "target_amount": Decimal("0")})
                        bucket["target_rows"] += 1
                        if _is_valid_amount(row["target_amount"]):
                            bucket["target_amount"] += Decimal(str(row["target_amount"]))
        start += page_size
    mirror_count = frappe.db.count(
        TARGET_DOCTYPE,
        filters={"legacy_entry_name": ["is", "set"]},
    )
    totals["orphaned_mirror_rows"] = max(0, mirror_count - totals["mirrored_rows"])
    scopes = []
    for company, currency in sorted(set(source_by_scope) | set(target_by_scope)):
        source = source_by_scope.get((company, currency), {})
        target = target_by_scope.get((company, currency), {})
        source_amount = source.get("source_amount", Decimal("0"))
        target_amount = target.get("target_amount", Decimal("0"))
        scopes.append({
            "company": company,
            "currency": currency,
            "source_rows": source.get("source_rows", 0),
            "source_amount": str(source_amount),
            "submitted_target_rows": target.get("target_rows", 0),
            "submitted_target_amount": str(target_amount),
            "difference": str(target_amount - source_amount),
        })
    totals["company_currency_totals"] = scopes
    totals["reconciliation_ready"] = bool(
        totals["source_rows"] == totals["mirrored_rows"]
        and totals["verified_rows"] == totals["source_rows"]
        and totals["mirror_mismatches"] == 0
        and totals["content_mismatches"] == 0
        and totals["lifecycle_incomplete_rows"] == 0
        and totals["orphaned_mirror_rows"] == 0
        and totals["unmappable_rows"] == 0
        and all(Decimal(scope["difference"]) == 0 for scope in scopes)
    )
    return totals


def _apply_migration_workflow(doc, action):
    """Apply historical workflow states under an explicit migration context."""
    from frappe.model.workflow import apply_workflow

    previous = getattr(frappe.flags, "in_cash_movement_migration", False)
    frappe.flags.in_cash_movement_migration = True
    try:
        return apply_workflow(doc, action)
    finally:
        frappe.flags.in_cash_movement_migration = previous


def _historical_approval_actions(source):
    """Return valid Frappe Workflow actions for a legacy approval state."""
    if source.approval_status == "Rejected" or source.status == "Cancelled":
        return ("Request Approval", "Reject")
    if source.approval_status == "Pending" or source.status == "Pending":
        return ("Request Approval",)
    return ("Request Approval", "Approve")


def mirror_legacy_movements(commit=False, limit=500, start=0):
    """Create a bounded page of mirrors. Re-running is safe and never mutates source rows.

    The call returns the full read-only census. With ``commit=True``, verified
    historical completed rows are submitted as mirrors, pending rows remain
    drafts, and rows lacking provable currency remain unverified drafts.
    ``start`` plus ``limit`` make the operation resumable after interruption.
    """
    report = census(start=start, limit=limit)
    page = report["rows"]
    inserted = []
    if not commit:
        report["planned"] = page
        report["writes_performed"] = False
        return report

    conflicts = [
        row["source"] for row in page
        if row["target"] and row["target_content_matches_source"] is False
    ]
    if conflicts:
        frappe.throw(
            "Migration stopped before writing because existing mirrors differ from source rows: "
            + ", ".join(conflicts)
        )

    for item in page:
        if item["target"] or not item.get("mappable", True):
            continue
        source = frappe.get_doc(SOURCE_DOCTYPE, item["source"])
        if not item["company"] or not _is_positive_amount(source.amount) or not source.entry_date or source.direction not in ("In", "Out"):
            continue
        party_type, party_name = _resolve_counterparty(source.counterparty)
        reversal_target = _submitted_mirror_for_legacy(source.reversal_of)
        completed = source.status in ("Completed", "Settled", "Reversed")
        values = {
            "doctype": TARGET_DOCTYPE,
            "owner": source.owner,
            "movement_date": source.entry_date,
            "company": item["company"],
            "source_cash_box": item["source_cash_box"],
            "destination_cash_box": item["destination_cash_box"],
            "direction": "Out" if source.other_box else source.direction,
            "amount": str(Decimal(str(source.amount))),
            "currency": item["currency"],
            "category": source.category or "Other",
            "money_flow": source.money_flow,
            "funding_source": source.funding_source if source.funding_source in FUNDING_SOURCES else None,
            "counterparty_type": party_type,
            "counterparty": party_name,
            "counterparty_text": source.counterparty if source.counterparty and not party_name else None,
            "variance_type": source.variance_type if source.variance_type != "None" else None,
            "variance_amount": source.variance_amount,
            "purpose": source.purpose,
            "notes": source.notes,
            "accounting_voucher_type": getattr(source, "reference_doctype", None) or None,
            "accounting_voucher": getattr(source, "reference_name", None) or None,
            "idempotency_key": f"legacy:{source.parent}:{source.name}",
            "legacy_entry_name": source.name,
            "migration_state": item["state"],
            "reversal_of": reversal_target,
        }
        doc = frappe.get_doc(values)
        doc.insert(ignore_permissions=True)
        if item["state"] == "Verified":
            # The Workflow has no Draft -> Approved transition: every
            # completed legacy row passes through the configured approval
            # state before it can be submitted.
            for action in _historical_approval_actions(source):
                _apply_migration_workflow(doc, action)
            # Submit completed history only when every fact needed to
            # group/sum it is verifiable and any reversal target exists.
            if completed and source.approval_status != "Rejected" and (not source.reversal_of or reversal_target):
                _apply_migration_workflow(doc, "Submit")
        inserted.append({"source": source.name, "target": doc.name, "state": item["state"], "docstatus": doc.docstatus})
    frappe.db.commit()
    report["inserted"] = inserted
    report["writes_performed"] = bool(inserted)
    return report


def _resolve_counterparty(raw_value):
    """Resolve only exact master names; never guess from a display-name substring."""
    raw = (raw_value or "").strip()
    if not raw:
        return None, None
    matches = []
    for doctype, field in (("Customer", "customer_name"), ("Supplier", "supplier_name"), ("Employee", "employee_name")):
        if frappe.db.exists(doctype, raw):
            matches.append((doctype, raw))
        elif field and frappe.db.exists(doctype, {field: raw}):
            names = frappe.get_all(doctype, filters={field: raw}, pluck="name", limit=2)
            if len(names) == 1:
                matches.append((doctype, names[0]))
    return matches[0] if len(matches) == 1 else (None, None)


def _submitted_mirror_for_legacy(legacy_entry_name):
    """Return a mirrored target only when it is submitted and safe to reverse."""
    if not legacy_entry_name:
        return None
    target = frappe.db.get_value(
        TARGET_DOCTYPE,
        {"legacy_entry_name": legacy_entry_name},
        ["name", "docstatus"],
        as_dict=True,
    )
    if not target or int(target.docstatus or 0) != 1:
        return None
    return target.name


def finalize_migrated_reversals(limit=500, start=0):
    """Link and submit previously mirrored reversal rows once their target exists."""
    pending = frappe.get_all(
        SOURCE_DOCTYPE,
        filters={"parenttype": "LaraTik Cash Box", "parentfield": "entries"},
        fields=["name", "reversal_of", "status"],
        order_by="name asc",
        limit_start=start,
        limit_page_length=limit,
    )
    completed = []
    for source in pending:
        if not source.reversal_of or source.status not in ("Completed", "Settled", "Reversed"):
            continue
        target_name = frappe.db.get_value(TARGET_DOCTYPE, {"legacy_entry_name": source.name}, "name")
        original_name = frappe.db.get_value(TARGET_DOCTYPE, {"legacy_entry_name": source.reversal_of}, "name")
        if not target_name or not original_name:
            continue
        original_docstatus = frappe.db.get_value(TARGET_DOCTYPE, original_name, "docstatus")
        if int(original_docstatus or 0) != 1:
            continue
        doc = frappe.get_doc(TARGET_DOCTYPE, target_name)
        if doc.docstatus == 0 and doc.migration_state == "Verified" and doc.workflow_state == "Approved":
            doc.reversal_of = original_name
            doc.save(ignore_permissions=True)
            _apply_migration_workflow(doc, "Submit")
            completed.append(target_name)
    frappe.db.commit()
    return {"submitted_reversals": completed, "writes_performed": bool(completed)}
