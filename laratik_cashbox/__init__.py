"""LaraTik Cashbox — the Frappe app that defines the operational cash model.

This package is the installable artefact that closes blocker H13. Until it
existed, the DocType definitions the Next.js app depends on lived nowhere: not
in this repository, not upstream. `docs/ERPNEXT_CUSTOM_DOCTYPE_DEPLOYMENT.md`
is the runbook that installs it.

The package version is used by Frappe's app-version reporting, and it is the
RELEASE version of this app: the number a customer sees on their site's
`Installed Application` record, and the number the distribution repository is
tagged with.

It is ONE number for the whole product — the Next.js app carries the same one
in its `package.json`, and `app-distribution-conformance` asserts they agree,
because the two halves are released together and the vendored schema corpus
is generated from this tree. A release that bumped one and not the other
would ship a UI that reads a schema it was not built against.
"""

__version__ = "0.1.0"
