"""Keep the INTENDED scope owner across Frappe's insert lifecycle.

─── WHY THE FIELD IS CALLED `scope_owner` AND NOT `owner` ───────────────────

Two upstream facts make the obvious implementation impossible:

1. **`owner` is a restricted fieldname.** Declaring a field called `owner` in
   a DocType JSON raises `InvalidFieldNameError` at `bench migrate`. The
   DocType cannot own a field with that name.

2. **Frappe adds a standard `owner` field to every doctype automatically**, and
   on insert `Document.set_user_and_timestamp()` executes:

   ```python
   if self.is_new() and not (self.flags.in_install):
       self.creation = self.modified
       self.owner = self.modified_by
   ```

   `self.owner` is therefore OVERWRITTEN with the session user. Every request
   the LaraTik Cashbox app makes to ERPNext arrives as the SAME integration
   user, so without a fix every `LaraTik *` record would be owned by the
   integration user — and the app's own scope filter, which reads `owner` and
   compares it with the end user's login id, would match nobody. The failure
   is DENY-ALL (nobody sees the record, including its author), not a
   cross-user leak: the filter is applied to the row as read, and the session
   identity is the end user's own login id.

So this app declares a SEPARATE, non-restricted field — `scope_owner` (Data) —
and restores the standard `owner` from it.

─── WHY `scope_owner` IS A REAL, QUERYABLE FIELD ───────────────────────────

Because Frappe's `owner` is a **Link to User**, it can only hold the login id
of a User that exists on the instance: an arbitrary email fails link
validation and aborts the insert. `scope_owner` is a plain `Data` column, so
it can hold any login id, and it is directly filterable:

    ["scope_owner", "=", "someone@example.invalid"]

That is strictly more expressive than Frappe's set-once `owner`, and it is the
field an operator should look at when asking "whose record is this?".

─── THE MECHANISM, AND WHY IT MUST BE `set_user_and_timestamp` ─────────────

The intended owner arrives in the request body under the key `owner`. The only
point in the insert lifecycle at which that value is still readable is BEFORE
`set_user_and_timestamp()` overwrites it — so the capture has to happen
inside an override of that method, before `super()` runs:

    insert() → set_user_and_timestamp() → before_insert → _validate_links()
             → _validate() → validate

`before_insert` and `validate` are too late: by then `self.owner` already
holds the session user, and the payload's value is gone. Overriding
`set_user_and_timestamp` is the standard Frappe pattern for this exact
problem, and it is the only hook that sees the value before it is lost.

─── WHY THE `owner` RESTORE IS CONDITIONAL ─────────────────────────────────

`owner` is Link to User, so assigning it a value that is not a User on this
instance makes Frappe's link validation reject the INSERT — the write fails
outright, which is a worse failure than the one being fixed. So:

* `scope_owner` ALWAYS receives the intended owner. It never fails, and it is
  the field the truth lives in.
* the standard `owner` is restored ONLY when the intended owner names a real
  User on this instance. When it does not, Frappe's own assignment stands and
  the app's own `StoreOwnershipError` fires on the first write — loudly, with
  a message that says exactly this.

That failure is deliberate and pre-existing: `src/lib/ai/store/scope.ts`
raises `StoreOwnershipError` when the row that comes back from ERPNext names
a different owner than the one that was asked for. An operator who sees it is
being told the truth, and the fix is to create the User — not to have the
schema quietly pretend the record is owned by the integration user.
"""

import frappe


class ScopeOwnerMixin:
	"""Mixin that carries the intended scope owner on insert.

	Mixed in BEFORE `frappe.model.document.Document` so that the mixin's
	`set_user_and_timestamp` is the one Frappe calls:

	    class LaraTikCollection(ScopeOwnerMixin, Document)

	With the reverse order `Document.set_user_and_timestamp` would win, the
	mixin would never run, and the override would be dead code that reads as
	if it worked.
	"""

	def set_user_and_timestamp(self):
		"""Capture the intended owner, then let Frappe do its own bookkeeping.

		Order matters and is the whole point:

		1. Read the intended owner from `scope_owner` if the caller sent one,
		   otherwise from the `owner` key in the request body. At this moment
		   the body's value is still intact.
		2. Call `super()`, which stamps `modified`/`modified_by` and — for a new
		   document — overwrites `owner` with the session user.
		3. Put the intended owner back, into `scope_owner` always and into
		   `owner` when that value names a User on this instance.
		"""
		intended = (self.get("scope_owner") or self.get("owner") or "").strip()

		super().set_user_and_timestamp()

		if not intended:
			return

		# The truth, in a column that can hold any login id.
		self.scope_owner = intended

		# The standard field, restored only when it can legally hold the
		# value. `owner` is Link to User; assigning it a login id that is not
		# a User here fails link validation and rejects the whole insert.
		if frappe.db.exists("User", intended):
			self.owner = intended

	def validate(self):
		"""Guarantee `scope_owner` is populated, even for a row created outside
		the app (a desk insert, a data import).

		`set_user_and_timestamp` has already run by the time `validate` runs,
		so `self.owner` is the session user here — which is the correct
		answer for a record nobody scoped on purpose, and the only answer
		available. The app's own writes always arrive with an `owner` in the
		body and are captured earlier, by the override above.

		`scope_owner` is deliberately NOT a mandatory field: Frappe's
		mandatory check may run before this hook, and a field that is
		mandatory but populated by a hook is a field that fails inserts for
		no reason.
		"""
		parent_validate = getattr(super(), "validate", None)
		if callable(parent_validate):
			parent_validate()

		if not (self.get("scope_owner") or "").strip():
			self.scope_owner = (self.get("owner") or "").strip()
