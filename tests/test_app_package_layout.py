"""The files loaded by Frappe must be importable submodules of the app package."""

from __future__ import annotations

import importlib.util
import os


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(APP_DIR, "laratik_cashbox")


def test_frappe_runtime_modules_are_inside_the_python_package():
    for module in ("hooks", "api", "migration", "patches.v1_0.ensure_doctypes"):
        path = os.path.join(PACKAGE_DIR, *module.split(".")) + ".py"
        assert os.path.isfile(path), f"laratik_cashbox.{module} must be packaged at {path}"
        assert importlib.util.find_spec(f"laratik_cashbox.{module}") is not None

    module_init = os.path.join(PACKAGE_DIR, "laratik_cashbox", "__init__.py")
    controller = os.path.join(PACKAGE_DIR, "laratik_cashbox", "doctype", "laratik_cash_box", "laratik_cash_box.py")
    assert os.path.isfile(module_init)
    assert os.path.isfile(controller)
    assert importlib.util.find_spec("laratik_cashbox.laratik_cashbox") is not None


def test_package_and_frappe_hook_paths_use_the_same_namespace():
    with open(os.path.join(PACKAGE_DIR, "hooks.py"), encoding="utf-8") as handle:
        hooks = handle.read()

    assert '"laratik_cashbox.erpnext_version.assert_supported_erpnext"' in hooks
    assert '"laratik_cashbox.patches.v1_0.ensure_doctypes.after_install"' in hooks

