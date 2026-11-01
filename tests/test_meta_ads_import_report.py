"""The guided import: what a file is checked against, and what is stored.

These tests hold the behaviour that Stage 3e changed:

1. A **reversal** is stored as a reversal — not as a refund, and not as
   "needs review".
2. The parser **reports every problem** in the file rather than refusing at
   the first bad row, so the operator can be told all of them at once.
3. The import is still **all-or-nothing**: a file that does not reconcile
   with its own section totals is never partly stored.
4. A row refused for a reason other than its **amount** still counts towards
   the file's own total, because the file counted it.
5. The settlement position nets a reversal, and the reversal is named apart
   from a refund.
"""

from __future__ import annotations

import base64

import pytest

from test_meta_ads_billing import FrappeError, billing  # noqa: F401  — the fixture


def invoice_summary(rows, *, account="act_123", total=None, currency="EUR"):
    """Meta's current invoice-summary layout, with one payment method per row."""
    lines = [
        f"Account: {account},,,,",
        "Advertiser Information",
        "Meta ads payment",
        "Date,Transaction ID,Payment method,Amount,Currency",
    ]
    lines.extend(
        f"31/10/2026,{transaction_id},{method},{amount},{currency}"
        for transaction_id, method, amount in rows
    )
    if total is None:
        total = sum(float(amount) for _, _, amount in rows)
    lines.append(f",,Total amount billed,{total:.2f},{currency}")
    return "\n".join(lines).encode("utf-8")


def encode(csv_bytes):
    return base64.b64encode(csv_bytes).decode("ascii")


# ─── A REVERSAL ───────────────────────────────────────────────────────────────

def test_a_reversal_is_stored_as_a_reversal(billing):
    module, fake = billing
    content = invoice_summary([("txn-rev", "Reversal", "30.00")])

    result = module.import_billing_report(
        "Company A", "Meta Account A", "meta.csv", encode(content)
    )

    stored = [row for row in fake.inserted if row.doctype == "LaraTik Meta Ads Payment"]
    assert len(stored) == 1
    assert stored[0].record_kind == "Reversal"
    assert result["reversal_total"] == "30"


def test_a_negative_reversal_is_a_reversal_and_not_a_refund(billing):
    module, fake = billing
    content = invoice_summary([("txn-rev", "Reversal", "-30.00")], total=-30.00)

    module.import_billing_report(
        "Company A", "Meta Account A", "meta.csv", encode(content)
    )

    stored = [row for row in fake.inserted if row.doctype == "LaraTik Meta Ads Payment"]
    assert stored[0].record_kind == "Reversal"


def test_the_preview_names_the_reversal_apart_from_a_refund(billing):
    module, _fake = billing
    content = invoice_summary([
        ("txn-pay", "Visa", "40.00"),
        ("txn-rev", "Reversal", "30.00"),
    ], total=70.00)

    preview = module.preview_billing_report(
        "Company A", "Meta Account A", "meta.csv", encode(content)
    )

    assert preview["reversal_count"] == 1
    assert preview["reversal_total"] == "30"
    assert preview["refund_count"] == 0
    assert preview["payment_total"] == "40"


# ─── THE PROBLEMS ─────────────────────────────────────────────────────────────

def test_the_parser_reports_a_bad_row_instead_of_dying_at_it(billing):
    module, fake = billing
    content = invoice_summary([
        ("txn-1", "Visa", "10.00"),
        ("txn-2", "Visa", "not-a-number"),
        ("txn-3", "Visa", "5.00"),
    ], total=15.00)

    parsed = module._parse_report(content, fake.account)
    codes = [problem["code"] for problem in parsed["problems"]]

    assert "INVALID_AMOUNT" in codes
    # The good rows are still read: the operator sees the whole file.
    assert parsed["counts"]["Payment"] == 2


def test_the_preview_lists_every_problem_with_its_line(billing):
    module, fake = billing
    content = invoice_summary([
        ("txn-1", "Visa", "10.00"),
        ("txn-2", "Visa", "not-a-number"),
        ("txn-3", "Visa", "also-not-a-number"),
    ], total=10.00)

    preview = module.preview_billing_report(
        "Company A", "Meta Account A", "meta.csv", encode(content)
    )

    assert preview["problem_count"] >= 2
    assert preview["invalid_row_count"] >= 2
    assert preview["can_import"] is False
    assert all("row_number" in problem for problem in preview["problems"])


def test_a_file_with_problems_is_not_imported_at_all(billing):
    module, fake = billing
    content = invoice_summary([
        ("txn-1", "Visa", "10.00"),
        ("txn-2", "Visa", "not-a-number"),
    ], total=10.00)

    with pytest.raises(FrappeError, match="was not imported"):
        module.import_billing_report(
            "Company A", "Meta Account A", "meta.csv", encode(content)
        )

    assert fake.inserted == []
    assert fake.saved_files == []


def test_a_refused_row_still_counts_towards_the_files_own_total(billing):
    """The file counted the row, so the section is reconciled against it.

    A row whose DATE is unreadable is refused, but its amount is readable and
    the file's total includes it. Without this, the refused row would look
    like money missing from the file.
    """
    module, fake = billing
    lines = [
        "Account: act_123,,,,",
        "Advertiser Information",
        "Meta ads payment",
        "Date,Transaction ID,Payment method,Amount,Currency",
        "31/10/2026,txn-good,Visa,10.00,EUR",
        "not-a-date,txn-bad,Visa,5.00,EUR",
        ",,Total amount billed,15.00,EUR",
    ]
    content = "\n".join(lines).encode("utf-8")

    parsed = module._parse_report(content, fake.account)
    codes = [problem["code"] for problem in parsed["problems"]]

    assert "INVALID_DATE" in codes
    # The section still reconciles: the refused row's amount is counted.
    assert "SECTION_TOTAL_MISMATCH" not in codes
    assert parsed["section_totals_verified"] is True


def test_a_row_whose_amount_is_unreadable_makes_the_section_unverifiable(billing):
    module, fake = billing
    lines = [
        "Account: act_123,,,,",
        "Advertiser Information",
        "Meta ads payment",
        "Date,Transaction ID,Payment method,Amount,Currency",
        "31/10/2026,txn-good,Visa,10.00,EUR",
        "31/10/2026,txn-bad,Visa,not-a-number,EUR",
        ",,Total amount billed,10.00,EUR",
    ]
    content = "\n".join(lines).encode("utf-8")

    parsed = module._parse_report(content, fake.account)
    codes = [problem["code"] for problem in parsed["problems"]]

    assert "INVALID_AMOUNT" in codes
    # The section cannot be verified, and it is NOT reported as a mismatch —
    # the missing amount is a consequence of the unreadable row.
    assert "SECTION_TOTAL_UNVERIFIED" in codes
    assert "SECTION_TOTAL_MISMATCH" not in codes
    assert parsed["section_totals_verified"] is False


# ─── THE PREVIEWED ROWS ───────────────────────────────────────────────────────

def test_the_preview_names_what_would_happen_to_each_row(billing):
    module, fake = billing
    content = invoice_summary([
        ("txn-1", "Visa", "10.00"),
        ("txn-2", "Ad Credit", "0.15"),
    ], total=10.15)

    preview = module.preview_billing_report(
        "Company A", "Meta Account A", "meta.csv", encode(content)
    )

    assert preview["row_count"] == 2
    assert preview["new_row_count"] == 2
    assert preview["can_import"] is True
    assert [row["status"] for row in preview["rows"]] == ["new", "new"]
    assert [row["record_kind"] for row in preview["rows"]] == ["Payment", "Credit"]


def test_a_row_already_stored_is_a_duplicate_and_not_stored_again(billing):
    module, fake = billing
    content = invoice_summary([("txn-1", "Visa", "10.00")], total=10.00)

    module.import_billing_report(
        "Company A", "Meta Account A", "meta.csv", encode(content)
    )
    stored = len([row for row in fake.inserted if row.doctype == "LaraTik Meta Ads Payment"])

    preview = module.preview_billing_report(
        "Company A", "Meta Account A", "meta.csv", encode(content)
    )

    assert stored == 1
    assert preview["duplicate_count"] == 1
    assert preview["new_row_count"] == 0
    assert preview["can_import"] is True
    assert preview["rows"][0]["status"] == "duplicate"
