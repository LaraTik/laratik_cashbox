# LaraTik Cashbox

A Frappe app for **ERPNext 16**: operational cash custody — cash boxes, cash
movements, and the balances that follow from them.

It is the data model underneath
[LaraTik Finance](https://github.com/LaraTik/laratik-finance), and it installs
on **any** ERPNext 16 instance. It requires `erpnext` and nothing else: no
LaraTik platform app, no external service.

---

## Requirements

| | |
|---|---|
| ERPNext | `>=16.0.0,<17.0.0` |
| Frappe | `>=16.0.0,<17.0.0` |
| Python | `>=3.14` |

The ERPNext constraint is enforced twice: declaratively in `pyproject.toml`
(which bench reads, and which Frappe Cloud reads), and procedurally in
`before_install` (which refuses the install before a single DocType is
created). **A wrong ERPNext version fails with one clear sentence** instead of
half-creating a schema.

---

## Install

On your ERPNext 16 bench:

```bash
# 1. fetch the app
bench get-app https://github.com/LaraTik/laratik_cashbox --branch v0.1.0

# 2. install it on the site
bench --site <site> install-app laratik_cashbox

# 3. run the migrations
bench --site <site> migrate

# 4. make the new doctypes visible to the desk and the API
bench --site <site> clear-cache
```

### Verify

```bash
bench --site <site> execute frappe.client.get_list \
  --kwargs '{"doctype":"LaraTik Cash Box","limit_page_length":1}'
```

An empty list is a successful install with no cash boxes yet. A
`DoesNotExistError` means the app did not install — check
`bench --site <site> list-apps`.

---

## Upgrade

```bash
bench get-app https://github.com/LaraTik/laratik_cashbox --branch v0.2.0
bench --site <site> migrate
```

`bench migrate` runs the patches in `laratik_cashbox/patches.txt`, in order,
once. They all run in `[post_model_sync]` — after the DocType sync — so they
check the schema the site actually has. **Every patch is idempotent**, so
re-running a migrate changes nothing the second time.

**Read [CHANGELOG.md](CHANGELOG.md) before you upgrade.** It lists, per
version, the patches that version carries. A version that changes the schema
carries a patch; a version that does not, does not — so the changelog tells
you whether `bench migrate` will touch your data or only re-verify your
schema.

**Back up first.** A migration that touches posted documents is not reversible
in place; the only rollback is a full restore.

```bash
bench --site <site> backup
```

---

## What it installs

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

---

## Permissions

The app creates no roles and grants nothing on its own. A user needs a role
that can read and write the cash-box doctypes — in practice Accounts Manager
and HR Manager for the company in question. **Scope is by company**: a user
without access to a company sees a refusal, never an empty list.

---

## Verified against

ERPNext **16.50.0** on Frappe 16.52.0, Python 3.14.8 — a local instance,
with the app installed on a real site and a real permission model, seeded, and
the repository's live verification gate run against it.

A local verification proves the **code** against a real ERPNext v16. It does
not verify a **deployment**. Nothing in this release has been read from a
customer's production instance.
