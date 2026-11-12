"""The distributed tool has one name across UI, launchers and storage."""

from pathlib import Path
import re
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from services.export_service import CONTROL_DIRECTORY
from services.profile_service import APP_DIR_NAME


TOOL_ROOT = Path(__file__).resolve().parents[2]
TOOL_NAME = "关键结构生成工具"


class ToolIdentityTests(unittest.TestCase):
    def test_storage_and_distributed_paths_use_the_current_name(self):
        self.assertEqual(APP_DIR_NAME, TOOL_NAME)
        self.assertEqual(CONTROL_DIRECTORY, "." + TOOL_NAME)
        self.assertEqual(TOOL_ROOT.name, TOOL_NAME)
        self.assertTrue((TOOL_ROOT / f"启动{TOOL_NAME}.command").is_file())

    def test_browser_title_and_brand_use_the_current_name(self):
        html = (TOOL_ROOT / "frontend/index.html").read_text(encoding="utf-8")
        self.assertIn(f"<title>{TOOL_NAME}</title>", html)
        brand = re.search(r'<a class="brand"[^>]*>(.*?)</a>', html, re.S).group(1)
        self.assertEqual(re.sub(r"<[^>]+>", "", brand).strip(), TOOL_NAME)

    def test_source_docs_and_filenames_do_not_contain_retired_name(self):
        retired = re.compile(r"关键结构生成\s*ai\s*工具", re.I)
        for path in TOOL_ROOT.rglob("*"):
            self.assertIsNone(retired.search(path.name), str(path))
            if path.is_file() and path.suffix in {".py", ".js", ".mjs", ".html", ".md", ".json", ".command"}:
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(retired.search(text), str(path))


if __name__ == "__main__":
    unittest.main()
