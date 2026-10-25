"""The ERPNext version this app supports, and the check that enforces it.

─── WHY THIS EXISTS ───────────────────────────────────────────────────────────

The app declared `required_apps = ["erpnext"]` and nothing else. That says
WHICH app it needs. It says nothing about WHICH VERSION — so the repository
did not record the ERPNext it was written against, and a bench running any
version at all would accept the install. Nothing anywhere would have told an
operator their ERPNext was wrong until something failed in a way that pointed
nowhere near the cause.

The target is **ERPNext v16**. This module states that in the one place that
cannot drift from the code, and refuses the install on anything else.

─── WHY THE CHECK LIVES HERE AND NOT ONLY IN pyproject.toml ───────────────────

The standard mechanism is `[tool.bench.frappe-dependencies]` in the app's
`pyproject.toml`, and bench enforces it at install time. That file now
exists, at the app ROOT — the directory bench calls `apps/<app>/`, which is
this directory:

    laratik_cashbox/            <- the app ROOT. pyproject.toml is HERE.
    └── laratik_cashbox/        <- the app PACKAGE.

A `pyproject.toml` INSIDE the package would be a file bench never reads: a
file that LOOKS like an enforced constraint and enforces nothing, which is
worse than no constraint at all. So it is at the root, and
`tests/test_packaging_metadata.py` asserts both halves — that it is at the
root, and that it is NOT inside the package.

So there are now two statements of the same constraint: the declarative one
in `pyproject.toml`, and this one, in code.

THEY DO DIFFERENT JOBS, AND BOTH ARE KEPT.

The declarative one gives the friendly error, before anything is installed,
and it is what Frappe Cloud reads — without it the app cannot be added to a
Cloud bench at all.

This one is the BACKSTOP. `before_install` calls `assert_supported_erpnext`,
which refuses the install on the wrong ERPNext before a single DocType is
created — so a bench on the wrong version fails in one clear sentence
instead of half-creating a schema and leaving it behind. It runs even when
the app is installed by hand rather than by bench.

They are the SAME STRING, and `tests/test_packaging_metadata.py` asserts
that, so they cannot drift apart: two constraints in two places that
disagree would give the operator one error from bench and a different one
at install time, and neither of them would say which is right.

─── WHY THE VERSION PARSING IS ITS OWN FUNCTION ──────────────────────────────

`check_erpnext_version` takes a version STRING and does the comparison. A
separate function does the reading. That split is what lets this be tested
with no site, no database, no bench, and no ERPNext installed at all — the
same discipline the rollup tests follow, and the reason the Python suite can
run in CI at all.
"""

from __future__ import annotations

import re

#: The ERPNext line this app supports. Named, so there is exactly ONE place to
#: change when a new line ships — and exactly one place to look when it does.
SUPPORTED_ERPNEXT_MAJOR = 16

#: What a human is told when the version is wrong. Written the way a version
#: constraint is written, so it can be copied into a `pyproject.toml` verbatim
#: when the bench layout is settled.
SUPPORTED_ERPNEXT_RANGE = ">=16.0.0,<17.0.0"

_MAJOR = re.compile(r"^\s*v?(\d+)")


class UnsupportedERPNext(Exception):
    """Raised when the installed ERPNext is not the one this app supports."""


def major_version(version: str) -> int:
    """The major number of an ERPNext version string.

    An ERPNext version looks like `16.28.1`. A development build may carry a
    `v` in front of it, so that is tolerated. A string with no leading number
    in it is not a version this app can reason about, and saying so is better
    than guessing a major from somewhere in the middle of it.
    """
    match = _MAJOR.match(version or "")
    if not match:
        raise UnsupportedERPNext(
            f"Could not read an ERPNext version from {version!r}. "
            f"This app supports ERPNext {SUPPORTED_ERPNEXT_RANGE}."
        )
    return int(match.group(1))


def check_erpnext_version(version: str) -> None:
    """Raise `UnsupportedERPNext` unless `version` is a supported ERPNext.

    The comparison is on the MAJOR version, not the full string. A patch
    release of a supported line is supported: `16.28.1` and `16.0.0` are the
    same ERPNext to this app, and refusing one of them would be a gate that
    cries wolf.
    """
    major = major_version(version)
    if major != SUPPORTED_ERPNEXT_MAJOR:
        raise UnsupportedERPNext(
            f"This app supports ERPNext {SUPPORTED_ERPNEXT_RANGE}, and this "
            f"bench has ERPNext {version}. Install the app on a v16 ERPNext, "
            f"or upgrade the bench first. The app has NOT been installed."
        )


def installed_erpnext_version() -> str:
    """The ERPNext version installed on this bench.

    Imported here, lazily, because the test suite must be able to import this
    module on a machine where ERPNext is not installed at all — which is the
    machine CI runs on.
    """
    import erpnext  # noqa: PLC0415 — imported late, on purpose

    return getattr(erpnext, "__version__", "")


def assert_supported_erpnext() -> None:
    """The `before_install` hook: refuse the install on the wrong ERPNext.

    Frappe calls this before it creates a single DocType, so a bench on the
    wrong version fails in one clear sentence instead of half-creating a
    schema and leaving it behind.
    """
    check_erpnext_version(installed_erpnext_version())
