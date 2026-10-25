"""`LaraTik Item` — one content row inside a `LaraTik Collection`.

A message, a tool execution, an action event and a memory statement are all
items. The row's `content` holds the RAW provider content-block array,
serialised, because a flattened transcript cannot reconstruct a valid
follow-up request: the `text`, `thinking` and `tool_use` blocks, with their
exact ids, have to be replayable verbatim.

─── WHY THIS DOCTYPE HAS NO `scope_owner` ───────────────────────────────────

Every other `LaraTik *` doctype carries one, because the application writes
an `owner` on them and reads it back. This one does not: the application
never writes an owner on an item, and never reads one. An item's visibility
is the visibility of its COLLECTION — the store resolves the collection
first, checks the caller's scope against it, and only then reads the rows
(`authoriseAppend` in `src/lib/ai/store/items.ts`).

Adding a `scope_owner` here would be a column nothing writes and nothing
reads, and a column that nothing reads is a column that is trusted to be
right.

─── WHY `metadata` AND `content` ARE `Long Text` AND NOT A JSON COLUMN ─────

The application serialises both with `JSON.stringify` and parses them with
`JSON.parse` over a STRING. A `Long Text` column round-trips that string
byte for byte, and the read side cannot drift. A native JSON column, on the
other hand, may hand back a PARSED OBJECT — and the item's own parser
(`parseStringList`/`parseMetadata` in the store) reads a string. The failure
that produces is silent: the blob is dropped and the row still looks fine.
"""

from frappe.model.document import Document


class LaraTikItem(Document):
	"""One content row. Thin on purpose — see the module docstring."""
