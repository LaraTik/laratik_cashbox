# LaraTik Cashbox — changelog

The versions of the Frappe app, and what each one changes on a site.

**This file ships with the app.** It is the customer's record of what an
upgrade did, and it is the only changelog a customer needs: the umbrella
`CHANGELOG.md` in the source repository records the *platform's shape* and
means nothing to someone running this app on their own ERPNext.

## How to read a version

Each version lists the patches it carries. Those patches are the
`[post_model_sync]` entries in `laratik_cashbox/patches.txt`, and `bench
migrate` runs them in that order, once, on the site.

**A version that changes the schema carries a patch; a version that does not,
does not.** That is the rule that makes an upgrade safe to run: a customer
reading this file knows whether `bench migrate` will touch their data or
only re-verify their schema.

---

## 0.1.13 — 2026-10-29

Active Meta ad accounts now receive a daily background refresh of the previous
four complete days in the account's own time zone. Starting on the fourth
local day of each month, the scheduler also retries a full previous-month sync
until a complete account and ad Insights run covers that period. Each account
is queued as a separate long job, and Paused accounts are skipped. The refresh
stores updated Insights snapshots only; payment evidence remains a separate
CSV import.

### Patches

None. No schema changed.

---

## 0.1.12 — 2026-10-29

This release clarifies finance and administration screen states and improves
phone navigation. The Frappe schema is unchanged.

### Changed

- Expenses and Budgets distinguish an empty chart/comparison from an empty
  register or a missing submitted cost-centre budget.
- Settings tabs stay on one phone-width row and remain reachable by swiping.
- Cashbox detail status and reimbursement-age labels use the selected locale.
- The web app and Frappe app both carry version `0.1.12`.

### Patches

None. No schema changed.

---

## 0.1.11 — 2026-10-29

This release expands the finance overview, reporting, and Meta Ads billing
workflows, with consistent navigation and responsive layouts across the
finance and administration screens. It also introduces the Meta Ads schema
used by those workflows.

### Changed

- Finance reports, projects, parties, invoices, employees, reimbursements,
  advances, AI records, and selected administration lists provide clearer
  scope and page navigation.
- Meta Ads spend, payment evidence, and setup are separate URL-addressable
  views. Incomplete or truncated payment evidence withholds totals.
- Cashbox, transfer, employee, billing, and report screens use compact mobile
  layouts while retaining detailed desktop tables.
- Cash counts use the linked account currency; unverified payroll and payment
  evidence remains distinct from posted accounting amounts.
- Shared route typography, card surfaces, table headers, and pagination now
  follow one dashboard hierarchy.
- The web app and Frappe app both carry version `0.1.11`.

### Patches

- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_page_asset`
- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_fee_rule`
- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_connection`
- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_billing_profile`
- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_account`
- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_spend`
- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_sync`
- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_payment_import`
- `laratik_cashbox.patches.v1_0.ensure_laratik_meta_ads_payment`

These patches verify the DocTypes after model sync. They do not create
accounting postings or alter existing posted vouchers.

---

## 0.1.10 — 2026-10-28

The amount formatter now safely handles harmless binary floating-point residue
from ERPNext currency conversions. The Frappe schema is unchanged.

### Changed

- Company-currency amounts are normalized through the shared exact-money
  adapter before formatting, preventing valid FX results from crashing a
  Server Component render.
- The version is `0.1.10` in both halves.

### Patches

None. No schema changed.

---

## 0.1.9 — 2026-10-26

The Payroll Center now presents complete available payroll history with
calendar-month navigation and a separate view of verified payroll settlements
by ledger posting date. Settlement amounts use the payroll payable account's
currency; salary cost remains grouped by the work month on each salary slip.
The Frappe schema is unchanged.

### Changed

- Payroll history can be filtered by month while preserving the selected month
  in the URL; the default view shows all available history.
- Gross and net salary trends follow salary-slip work months, while verified
  payroll settlements follow their actual ledger posting months.
- Employee totals show verified salary settlements to date, and payments without an
  employee attribution remain separate.
- Payroll history visuals have an exact-value desktop table and labeled phone
  and tablet cards; English and Arabic copy are kept in parity. The responsive
  header stays within the screen while keeping company selection and account
  access visible.
- Truncated or unreadable sources withhold affected totals and trends rather
  than presenting partial amounts as complete.
- The version is `0.1.9` in both halves.

### Patches

None. No schema changed.

---

## 0.1.8 — 2026-10-26

The application shell now displays the actual release version instead of a
stale hard-coded label. The Frappe schema is unchanged.

### Changed

- Desktop and mobile navigation now read the displayed version from the
  release package metadata.
- The version is `0.1.8` in both halves.

### Patches

None. No schema changed.

---

## 0.1.7 — 2026-10-26

The eighth release. The Frappe schema is unchanged; the web app now exposes
current salary setup and keeps paid-to-date figures truthful when historical
payout evidence has not been imported.

### Changed

- Employee payroll views show the current submitted salary assignment, its
  assigned base, effective date, and source without presenting the base as
  gross or net pay.
- Payroll salary payments shows current assigned bases for active employees.
- Paid-to-date is shown only from verified payroll settlement evidence; missing
  history is labelled as not imported rather than shown as zero.
- English and Arabic payroll copy remain in dictionary parity.
- The version is `0.1.7` in both halves.

### Patches

None. No schema changed.

---

## 0.1.6 — 2026-10-26

The seventh release. This release carries the company-wide physical-cash
view and keeps the browser directory free of server-only dependencies.

### Changed

- The pure physical-cash classification used by employee surfaces is now
  shared from a client-safe financial module.
- The version is `0.1.6` in both halves.

### Patches

None. No schema changed.

---

## 0.1.5 — 2026-10-26

The sixth release. The Frappe schema is unchanged; the web app now reports
company-wide physical cash across the Main and custody boxes.

### Changed

- Physical-cash totals include cash-rule custody boxes, keep currencies
  separate, and exclude Outstanding claim/loan boxes.
- Employee net-position totals exclude Outstanding claim/loan boxes.
- Unreadable or incomplete box reads withhold the affected figures.
- The version is `0.1.5` in both halves.

### Patches

None. No schema changed.

---

## 0.1.4 — 2026-10-25

The fifth release. The Frappe app changes nothing; the WEB app's setup wizard
does, and the two ship together because they are one version.

### Changed

- **The web app's "Chart of Accounts" picker is fed when the country is
  chosen.** The picker was filled from a list read ONCE with the page, using
  the country in the SAVED draft — so on a first visit, with no saved draft,
  it was an empty, disabled select, and choosing a country in the form did
  not change it. The list is now read when the country changes, and a failed
  read shows no charts rather than another country's.
- **The web app asks ERPNext for the same chart fallback ERPNext's own setup
  wizard asks for** (`with_standard`), so a country with exactly one regional
  chart no longer offers that chart alone.
- The version is `0.1.4` in both halves.

### Patches

None. No schema changed.

---

## 0.1.3 — 2026-10-25

The fourth release, and the one where the image actually says what it is.

### Changed

- **The build commit reaches the RUNNER stage.** A Docker build ARG belongs
  to the stage that declares it, and the first attempt declared it in the
  BUILDER stage — where it does not travel to the final image. The readiness
  probe kept answering `build.commit: null` for exactly that reason. The ARG
  is now declared where the image ships.
- The version is `0.1.3` in both halves.

### Patches

None. No schema changed.

---

## 0.1.2 — 2026-10-25

The third release, and the first one whose image can say what it is.

### Changed

- **The image reports its commit.** The readiness probe answered
  `build.commit: null` on a self-hosted image, because it read Vercel's
  variable name for the fact. `BUILD_COMMIT` is now set by the Docker build
  and read first, with Vercel's name kept as the fallback — so the running
  container can say which commit it is, and a deployment can be traced to the
  tree it came from.
- The version is `0.1.2` in both halves, for the same reason it always is:
  the corpus is generated from this app's tree.

### Patches

None. No schema changed.

---

## 0.1.1 — 2026-10-25

The second release, and the first one cut through the release machinery
end to end: the version bumped in both halves, the distributable repository
regenerated and tagged, the image built and published, the bench upgraded in
place, and the migration re-run.

### Changed

- The version is `0.1.1` in both halves. They move together because the web
  app's schema corpus is GENERATED from this app's tree, so a release that
  bumped one and not the other would ship a UI reading a schema it was not
  built against.
- The **web app** changed: the Desk's address is configuration rather than a
  literal, the app is served from the apex, and the health check is the
  READINESS probe (`/api/health/ready`) instead of the liveness one.
- The **app** itself did not change: no DocType, no patch, no schema. The
  version moved because the product moved, and because the two halves carry
  one version.

### Patches

None. This version changes no schema, so `bench migrate` has nothing to run
beyond the usual re-verification — which is exactly what the changelog is
for: a reader knows before upgrading whether the migration will touch their
data.

### Verified against

The distributable tree is byte-for-byte the published release (every file
checked against `DISTRIBUTION.json`), the bench was **recreated from
scratch** with the app mounted and still imported it, and the site migrated
and served the app against ERPNext 16.31.0.

---

## 0.1.0 — 2026-10-25

The first distributable release. Nothing here is a change from a previous
version — this is the first version of the app that can be installed on a
customer's ERPNext.

### Requires

| | |
|---|---|
| ERPNext | `>=16.0.0,<17.0.0` |
| Frappe | `>=16.0.0,<17.0.0` |
| Python | `>=3.14` |

The ERPNext constraint is enforced twice on purpose: declaratively in
`pyproject.toml` (which bench reads, and which Frappe Cloud reads), and
procedurally in `before_install` (which refuses the install before a single
DocType is created). The install fails on a wrong version with one clear
sentence rather than half-creating a schema.

### Installs

Ten DocTypes, in dependency order:

| DocType | What it is |
|---|---|
| `LaraTik Cash Box` | the cash box itself, with `balance_role` and its linked Account |
| `LaraTik Cash Box Entry` | one line of a cash box's movement |
| `LaraTik Cash Box Pending` | a movement that is entered but not yet settled |
| `LaraTik Cash Movement` | a standalone operational cash movement |
| `LaraTik Collection` | a client or counterparty |
| `LaraTik Item` | an item tracked by the cash model |
| `LaraTik Metric` | a derived measure |
| `LaraTik Artifact` | an artefact the model produced |
| `LaraTik Index Entry` | an index over the model's records |
| `LaraTik Setup State` | the record that says the setup is complete |

### Patches

Thirteen, all in `[post_model_sync]`, so they run **after** the DocType sync
and therefore check the schema the site actually has:

| # | Patch | What it does |
|---:|---|---|
| 1 | `ensure_laratik_cash_movement` | verifies the standalone movement schema |
| 2 | `install_cash_movement_workflow` | ensures the v16 approval Workflow and its standard action masters |
| 3 | `ensure_laratik_cash_box_entry` | verifies the child table |
| 4 | `ensure_laratik_cash_box_pending` | verifies the pending child table |
| 5 | `ensure_laratik_cash_box` | verifies the cash box |
| 6 | `backfill_cash_box_balance_role` | fills a blank `balance_role` with `Custody`; **never promotes a row** |
| 7 | `ensure_laratik_collection` | verifies the collection schema |
| 8 | `ensure_laratik_item` | verifies the item schema |
| 9 | `ensure_laratik_metric` | verifies the metric schema |
| 10 | `ensure_laratik_artifact` | verifies the artefact schema |
| 11 | `ensure_laratik_index_entry` | verifies the index schema |
| 12 | `backfill_laratik_cash_box_balances` | idempotently backfills derived balance columns |
| 13 | `verify_setup_state` | **fails the migrate** if the setup-state schema did not sync |

The `ensure_*` patches read the site's live meta and this app's JSON, and
fail the migration if they disagree. **The verification patch never writes** —
the schema reaches the site through the DocType sync, and a second writer
would be a second answer to the same question.

The two backfills are idempotent: running them twice changes nothing the
second time. That is what makes `bench migrate` safe to re-run.

### Verified against

ERPNext **16.50.0** on Frappe 16.52.0, Python 3.14.8 — a **local** instance,
with the app installed on a real site and a real permission model (System
Manager + Accounts Manager + HR Manager), seeded, and the repository's live
verification gate run against it.

A local verification proves the **code** against a real ERPNext v16. It does
not verify a **deployment**. Nothing in this release has been read from a
customer's production instance.
