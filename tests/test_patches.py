"""Every Frappe patch manifest entry resolves to an executable module.

Frappe's ``execute_patch`` treats a line in ``patches.txt`` as a module path
and imports ``<module>.execute``. A dotted function path can look plausible
but fails at migration time because Frappe then tries to import that function
as a nested module. This suite checks the same module contract without
importing Frappe or the patch modules.

Run with::

    cd laratik_cashbox && python3 -m pytest tests/test_patches.py -q
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(APP_DIR, "laratik_cashbox")
PATCHES_TXT = os.path.join(PACKAGE_DIR, "patches.txt")


def read_entries(path: str = PATCHES_TXT) -> list[str]:
    """Read manifest modules in their deliberate migration order."""
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()

    entries: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        entries.append(stripped)
    return entries


def module_path(dotted: str, app_dir: str = APP_DIR) -> str:
    """Resolve ``laratik_cashbox.patches.v1_0.example`` to its Python file."""
    parts = dotted.split(".")
    if not parts or parts[0] != "laratik_cashbox" or any(not part.isidentifier() for part in parts):
        raise ValueError(f"{dotted!r} is not a dotted Python module path")
    return os.path.join(app_dir, *parts) + ".py"


def top_level_functions(path: str) -> set[str]:
    """Read module-level function names without importing Frappe dependencies."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def resolve_patch_module(entry: str, app_dir: str = APP_DIR) -> str:
    """Return an executable patch module or explain why Frappe cannot run it."""
    if entry.startswith("execute:"):
        raise ValueError("inline execute patches are not allowed in this app")

    path = module_path(entry, app_dir)
    if not os.path.isfile(path):
        raise ValueError(f"{entry!r} names missing module {os.path.relpath(path, app_dir)}")

    functions = top_level_functions(path)
    if "execute" not in functions:
        raise ValueError(f"{entry!r} has no module-level execute() function")
    return path


ENTRIES = read_entries()


def test_the_manifest_is_not_empty():
    """A parser that finds nothing reports green having verified nothing."""
    assert ENTRIES, f"{PATCHES_TXT} has no patch entries"


def test_every_entry_is_a_valid_dotted_module_path():
    for entry in ENTRIES:
        module_path(entry)


@pytest.mark.parametrize("entry", ENTRIES)
def test_every_patch_module_exists_and_defines_execute(entry: str):
    """Frappe appends ``.execute`` to the module path while running a patch."""
    resolve_patch_module(entry)


def test_no_executable_patch_module_is_orphaned():
    """Every on-disk module that can run as a patch must be in the manifest."""
    named_modules = set(ENTRIES)
    patches_dir = os.path.join(PACKAGE_DIR, "patches")
    executable_modules = set()

    for folder, _dirs, files in os.walk(patches_dir):
        if "__pycache__" in folder:
            continue
        for name in files:
            if not name.endswith(".py") or name == "__init__.py":
                continue
            absolute_path = os.path.join(folder, name)
            if "execute" not in top_level_functions(absolute_path):
                continue
            relative = os.path.relpath(absolute_path, APP_DIR)
            dotted = relative[: -len(".py")].replace(os.sep, ".")
            executable_modules.add(dotted)

    orphans = sorted(executable_modules - named_modules)
    assert not orphans, (
        f"these executable patch modules are not named in patches.txt: {orphans}"
    )


def test_resolver_accepts_a_valid_execute_module(tmp_path: Path):
    """Control: a legal module path with a top-level execute() is accepted."""
    module = tmp_path / "laratik_cashbox" / "patches" / "v1_0" / "valid_patch.py"
    module.parent.mkdir(parents=True)
    module.write_text("def execute():\n    return None\n", encoding="utf-8")

    assert resolve_patch_module(
        "laratik_cashbox.patches.v1_0.valid_patch", str(tmp_path)
    ) == str(module)


def test_resolver_rejects_a_function_suffixed_like_a_module(tmp_path: Path):
    """Attack: a helper function suffix is not an importable patch module."""
    module = tmp_path / "laratik_cashbox" / "patches" / "v1_0" / "valid_patch.py"
    module.parent.mkdir(parents=True)
    module.write_text("def execute():\n    return None\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing module"):
        resolve_patch_module(
            "laratik_cashbox.patches.v1_0.valid_patch.execute", str(tmp_path)
        )


def test_resolver_rejects_a_module_that_cannot_run(tmp_path: Path):
    """A missing execute() or syntax error must fail the guard, never pass."""
    module = tmp_path / "laratik_cashbox" / "patches" / "v1_0" / "not_executable.py"
    module.parent.mkdir(parents=True)
    module.write_text("def helper():\n    return None\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no module-level execute"):
        resolve_patch_module(
            "laratik_cashbox.patches.v1_0.not_executable", str(tmp_path)
        )

    module.write_text("def execute(\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        resolve_patch_module(
            "laratik_cashbox.patches.v1_0.not_executable", str(tmp_path)
        )
