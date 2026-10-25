"""`LaraTik Artifact` — one ingested document in the copilot's library.

The text of a contract, a policy or a note, owned and scoped exactly like a
conversation: a document one company's user attached is not another company's
to read.

─── WHY `content_hash` IS HERE AND NOT OPTIONAL ────────────────────────────

It is the IDEMPOTENCY KEY. Re-ingesting the same document must not create a
second artifact and a second set of index entries, because a library that
grows a copy every time a file is re-attached is a library nobody can
search. The hash is of the CONTENT, so the same file attached twice is the
same artifact — which is why the application looks the artifact up by
`company` + `owner` + `content_hash` before it writes one
(`findByHash` in `src/lib/ai/store/artifacts.ts`).

─── WHY `scope_owner` AND NOT `owner` ──────────────────────────────────────

The application writes an `owner` on an artifact, filters on it, and reads it
back — and Frappe overwrites the standard `owner` on insert with the session
user, which for this application is a single integration user. The full
reasoning and the mechanism that keeps the intended owner alive are in
`laratik_cashbox/laratik_cashbox/scope_owner.py`.

─── WHY `occurred_on` IS A `Datetime` AND NOT A `Date` ─────────────────────

The field NAME says "on" — it was named for symmetry with `opened_on` and
`closed_on`, which ARE dates. But the application writes a TIMESTAMP into it
(`input.occurredAt`), and a `Date` column would silently truncate it to a
day. The type follows the data, not the name: a silently truncated timestamp
is the same class of defect as a silently coerced amount, and it is exactly
what this repository's conformance guards exist to prevent.
"""

from frappe.model.document import Document

from laratik_cashbox.scope_owner import ScopeOwnerMixin


class LaraTikArtifact(ScopeOwnerMixin, Document):
	"""One ingested document. Thin on purpose — see the module docstring."""
