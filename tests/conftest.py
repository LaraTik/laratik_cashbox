"""Load the same Python package path that a Frappe bench installs."""

from __future__ import annotations

import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
sys.modules.pop("laratik_cashbox", None)
import laratik_cashbox

assert Path(laratik_cashbox.__file__).resolve() == APP_ROOT / "laratik_cashbox" / "__init__.py"

