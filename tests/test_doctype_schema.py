"""Guard the Frappe app's on-disk DocType manifests before installation."""

import json
import configparser
from pathlib import Path


def test_every_app_doctype_manifest_is_a_doctype_record_in_its_module():
    doctype_root = Path(__file__).parents[1] / "laratik_cashbox" / "laratik_cashbox" / "doctype"
    manifests = sorted(doctype_root.glob("*/**/*.json"))

    assert len(manifests) == 10
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["doctype"] == "DocType", path
        assert manifest["module"] == "LaraTik Cashbox", path
        assert manifest["name"].casefold().replace(" ", "_") == path.parent.name, path


def test_patch_manifest_has_both_frappe_v16_sync_sections():
    patches = configparser.ConfigParser(allow_no_value=True)
    patches.read(Path(__file__).parents[1] / "laratik_cashbox" / "patches.txt", encoding="utf-8")

    assert patches.has_section("pre_model_sync")
    assert patches.has_section("post_model_sync")
