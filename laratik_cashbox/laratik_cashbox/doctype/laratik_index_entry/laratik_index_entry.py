"""`LaraTik Index Entry` — one searchable passage of a `LaraTik Artifact`.

The index is a separate doctype from the artifact because a document is read
whole by a person and a passage is matched a hundred times by the retriever.
Putting passages in a column on the document would mean re-reading every
document to search, and would make the searchable unit and the readable unit
the same thing — which is how a retriever ends up quoting half a page.

─── WHY `keywords` AND `embedding` ARE `Long Text` ─────────────────────────

Both are written as JSON TEXT by the application:

    keywords:  JSON.stringify(input.keywords.map((k) => k.toLowerCase()))
    embedding: JSON.stringify(input.embedding)   // or `null`

and both are read back by a parser that starts from a STRING
(`parseStringList` / `parseEmbedding` in `src/lib/ai/store/artifacts.ts`).
A `Long Text` column round-trips that string exactly. A native JSON column
may hand back a parsed ARRAY, which those parsers read as "no keywords" and
"no embedding" — a silent degradation with a confident-looking row.

`embedding` is `null` when there is none, and `null` is a WITHHELD value, not
an empty vector: an empty vector is a real thing that means "nothing", and a
caller that cannot tell it from "not computed" computes nonsense with it.

─── WHY `retrieval_mode` IS A `Select` ─────────────────────────────────────

Because the mode travels with every entry and the answer depends on it. The
embeddings endpoint is rate-limited on this application's credential, so the
library is keyword-only by default and says so per row. A retrieval layer
that PROMISES a semantic search and cannot deliver it is worse than one that
says what it is.

─── WHY `scope_owner` AND NOT `owner` ──────────────────────────────────────

The application writes an `owner` on an entry, filters on it, and reads it
back — and Frappe overwrites the standard `owner` on insert with the session
user, which for this application is a single integration user. The full
reasoning and the mechanism that keeps the intended owner alive are in
`laratik_cashbox/laratik_cashbox/scope_owner.py`.
"""

from frappe.model.document import Document

from laratik_cashbox.scope_owner import ScopeOwnerMixin


class LaraTikIndexEntry(ScopeOwnerMixin, Document):
	"""One searchable passage. Thin on purpose — see the module docstring."""
