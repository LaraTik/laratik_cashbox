"""Frappe app hooks for `laratik_cashbox`.

The app owns the LaraTik operational and support DocTypes and implements
controller validation, money rollups, movement lifecycle rules, scoped read
methods, and migration tooling. Scope-owner restoration remains a property of
the document controller (see `laratik_cashbox/scope_owner.py`).

There is deliberately NO `doc_events` entry for owner restoration. It must run
inside `set_user_and_timestamp`, before Frappe's own assignment; a `doc_events`
hook runs too late to undo the overwrite.
"""

app_name = "laratik_cashbox"
app_title = "LaraTik Cashbox"
app_publisher = "LaraTik"
app_description = (
    "Operational cash custody and support DocTypes for the LaraTik Cashbox "
    "application, including standalone cash movements, plus durable copilot state."
)
# A placeholder, deliberately: this is the address a bench shows as the
# app's contact, and it must never be a real mailbox that nobody watches.
app_email = "platform@example.invalid"
app_license = "MIT"

# ERPNext is required because `LaraTik Cash Box.cash_account` links to `Account` and
# `LaraTik Cash Box.employee` links to `Employee` — both ERPNext doctypes. Nothing
# else in this app depends on ERPNext, and nothing depends on HRMS: the
# `Employee` doctype it reads is ERPNext's, not HRMS's.
#
# `required_apps` names APPS and takes NO version specifier. A constraint
# written here would be silently ignored, which is why the version is
# enforced by the hook below rather than here.
required_apps = ["erpnext"]

# ─── THE ERPNext VERSION IS CHECKED, NOT ASSUMED ───────────────────────────────
#
# This app is written against ERPNext v16. Until this hook existed the
# repository said so NOWHERE: `required_apps` said "erpnext" and stopped
# there, and a bench running any version at all would have accepted the
# install. Nothing would have told an operator their ERPNext was wrong until
# something failed in a way that pointed nowhere near the cause.
#
# It runs BEFORE a single DocType is created, so a bench on the wrong
# version fails in one sentence instead of half-creating a schema.
#
# See `laratik_cashbox/erpnext_version.py` for why the check lives in
# code rather than in a `pyproject.toml`, and
# `docs/06-MIGRATION-STRATEGY.md` §10 for the install path on v16.
before_install = (
    "laratik_cashbox.erpnext_version.assert_supported_erpnext"
)

# Fresh installs mark existing patch entries complete, so required setup data
# such as the standalone movement Workflow must be created here as well as by
# the post-model-sync patch used during later upgrades.
after_install = "laratik_cashbox.patches.v1_0.ensure_doctypes.after_install"
