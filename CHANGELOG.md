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
