"""`LaraTik Metric` — a typed counter the copilot records about itself.

Requests, input tokens, output tokens, cache-read tokens, tool executions and
provider errors. The metric NAMES come from the application
(`AI_METRICS` in `src/lib/ai/contracts/store.ts`) and are open-ended by
design — the family is meant to grow a counter without a schema change — so
`metric` is a plain `Data` column and NOT a Select.

─── WHY `scope_owner` AND NOT `owner` ──────────────────────────────────────

The application writes an `owner` on a metric and reads it back, and Frappe
overwrites the standard `owner` on insert with the session user — a single
integration user for every request this app makes. The full reasoning and the
mechanism that keeps the intended owner alive are in
`laratik_cashbox/laratik_cashbox/scope_owner.py`.
"""

from frappe.model.document import Document

from laratik_cashbox.scope_owner import ScopeOwnerMixin


class LaraTikMetric(ScopeOwnerMixin, Document):
	"""One counter. Thin on purpose — see the module docstring."""
