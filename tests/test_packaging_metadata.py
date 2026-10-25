"""The app's packaging metadata, and the constraint it declares.

─── WHY THIS EXISTS ────────────────────────────────────────────────────────────

The app had NO `pyproject.toml`, so it declared NO version constraint in the
place bench reads one. The constraint was enforced in code only — which works,
but leaves two gaps:

* **Frappe Cloud refuses the app.** Its custom-app guide asks for "a valid
  `pyproject.toml` file along with a key advertising the frappe version",
  and without one the app cannot be added to a Cloud bench at all.
* **The constraint was invisible to the tooling.** `bench validate-dependencies`
  reads `[tool.bench.frappe-dependencies]`; with no file, there was nothing
  for it to check.

So the file is here now. And that creates a NEW risk, which is the one this
test exists to close: **two constraints, in two places, that can drift apart.**

The declarative one lives in `pyproject.toml`. The enforcing one lives in
`laratik_cashbox.erpnext_version.SUPPORTED_ERPNEXT_RANGE` — and it is the one
that runs, because `before_install` calls `assert_supported_erpnext` before a
single DocType is created.

If they disagree, the operator gets one error while bench validates and a
different one at install time, and neither of them says which is right. So
this asserts they are the SAME STRING.

─── WHY THE FILE IS AT THE APP ROOT, NOT INSIDE THE PACKAGE ───────────────────

bench reads the app root — the directory it calls `apps/<app>/`. That is
this directory:

    laratik_cashbox/            <- the APP ROOT. pyproject.toml is HERE.
    └── laratik_cashbox/        <- the app PACKAGE.

A `pyproject.toml` inside the package would be a file bench never reads: one
that LOOKS like an enforced constraint and enforces nothing, which is worse
than no constraint at all. So the test asserts the file is at the root AND
that it is NOT inside the package.
"""

from __future__ import annotations

import os
import tomllib


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(APP_DIR, "laratik_cashbox")
PYPROJECT = os.path.join(APP_DIR, "pyproject.toml")


def _pyproject() -> dict:
    with open(PYPROJECT, "rb") as handle:
        return tomllib.load(handle)


def test_pyproject_is_at_the_app_root_where_bench_reads_it():
    assert os.path.isfile(PYPROJECT), (
        f"the app root must carry a pyproject.toml — bench reads it from "
        f"`apps/<app>/`, and this directory IS that app root: {APP_DIR}"
    )
    inside_package = os.path.join(PACKAGE_DIR, "pyproject.toml")
    assert not os.path.exists(inside_package), (
        "a pyproject.toml inside the package would be a file bench never "
        "reads — one that looks like an enforced constraint and enforces nothing"
    )


def test_the_app_name_is_the_frappe_app_name():
    # bench reads `project.name` to know what the app is called, and Frappe
    # resolves `apps/<name>` and the python package by the same name. A
    # mismatch here is an app that installs and then cannot be found.
    assert _pyproject()["project"]["name"] == "laratik_cashbox"


def test_the_declared_erpnext_constraint_is_the_one_the_code_enforces():
    from laratik_cashbox.erpnext_version import SUPPORTED_ERPNEXT_RANGE

    declared = (
        _pyproject()
        .get("tool", {})
        .get("bench", {})
        .get("frappe-dependencies", {})
        .get("erpnext")
    )
    assert declared is not None, (
        "the app declares no `[tool.bench.frappe-dependencies].erpnext`, so "
        "bench has nothing to validate and Frappe Cloud has nothing to read"
    )
    assert declared == SUPPORTED_ERPNEXT_RANGE, (
        "the constraint declared in pyproject.toml and the constraint the code "
        "enforces are different strings, so the operator gets one error from "
        "bench and a different one at install time, and neither says which is "
        "right"
    )


def test_frappe_is_constrained_to_the_same_line():
    # Frappe's own version is what the app is installed against, and a v16
    # ERPNext implies a v16 Frappe. Declaring only erpnext would leave the
    # app's actual runtime unpinned.
    frappe = (
        _pyproject()
        .get("tool", {})
        .get("bench", {})
        .get("frappe-dependencies", {})
        .get("frappe")
    )
    assert frappe is not None, "the frappe version is not declared"
    assert frappe == (
        _pyproject()
        .get("tool", {})
        .get("bench", {})
        .get("frappe-dependencies", {})
        .get("erpnext")
    ), "frappe and erpnext are pinned to different lines"


def test_the_build_backend_is_the_one_frappe_v16_uses():
    # `frappe make-app` generates `flit_core >=3.4,<4` / `flit_core.buildapi`
    # — the same backend Frappe itself builds with. A different backend is a
    # different wheel layout, and the wheel is what a bench installs.
    build_system = _pyproject()["build-system"]
    assert build_system["build-backend"] == "flit_core.buildapi"
    assert any("flit_core" in requirement for requirement in build_system["requires"])


def test_the_python_requirement_matches_frappe_v16():
    # Frappe v16's own pyproject says `requires-python = ">=3.14"`, and its
    # `make-app` template says the same. An app that declares a lower floor
    # would install on a Python that cannot run the framework it depends on.
    requires_python = _pyproject()["project"]["requires-python"]
    assert requires_python == ">=3.14", (
        f"the app requires {requires_python}, but Frappe v16 requires >=3.14"
    )


def test_the_version_is_dynamic_and_the_package_carries_it():
    # flit reads the version from the package's `__version__`, so the
    # distribution version and the reported app version cannot disagree.
    project = _pyproject()["project"]
    assert project.get("dynamic") == ["version"], (
        "the version must stay dynamic, or it would be stated in two places "
        "— the pyproject and the package's __version__"
    )

    from laratik_cashbox import __version__

    assert __version__, "the package carries no __version__ for flit to read"
