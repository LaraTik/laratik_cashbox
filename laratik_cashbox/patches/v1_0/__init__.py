"""Upgrade patches, in order. One per doctype — see `patches.txt`.

Each patch is a VERIFICATION, not an applier: the schema reaches the site
through `bench migrate`'s DocType sync, and the patch's job is to fail the
migrate when that sync did not land. The reasoning is in
`ensure_doctypes.py`.
"""
