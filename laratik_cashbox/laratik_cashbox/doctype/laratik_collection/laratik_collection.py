"""`LaraTik Collection` — a container for the copilot's durable state.

A conversation, an action proposal, a memory store and an ingested document
are ALL collections: the family is deliberately generic, so a new AI feature
does not need a schema change.

─── WHY `scope_owner` AND NOT `owner` ──────────────────────────────────────

`owner` is a restricted fieldname — a DocType JSON that declares it fails to
migrate — and Frappe's standard `owner` is overwritten on insert with the
session user, which for this application is a single integration user. The
full reasoning, and the mechanism that keeps the intended owner alive, are in
`laratik_cashbox/laratik_cashbox/scope_owner.py`. Read that before
changing anything here.

─── WHY `company` IS `Data` AND NOT A LINK TO `Company` ────────────────────

`docs/AI-ERPNEXT-DOCTYPE-SPEC.md` proposes a Link to `Company` here. This app
uses a plain `Data` column, for a reason the spec does not consider: the
application scopes by a company NAME it is given, and a Link would make the
write fail whenever that name is not a `Company` record on this instance —
turning a scope label into a hard foreign key. The application filters on
this column and compares it for equality; it never resolves it.

─── WHAT ERPNEXT IS EXPECTED TO DO INSTEAD OF THIS CONTROLLER ──────────────

Nothing beyond the owner restoration. The collection's audience — the extra
people a `Restricted` collection names — lives in `metadata` under the key
`allowed`, as a JSON array of login ids, and the application reads it from
there.
"""

from frappe.model.document import Document

from laratik_cashbox.scope_owner import ScopeOwnerMixin


class LaraTikCollection(ScopeOwnerMixin, Document):
	"""One container. Thin on purpose — see the module docstring."""
