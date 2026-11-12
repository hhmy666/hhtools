"""Static and HTTP contracts for the standalone tower reducer shell."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[2]
ASSETS = TOOL_ROOT / "frontend" / "assets"
FONT_FILES = {
    "InterVariable.woff2",
    "INTER-LICENSE.txt",
    "SourceHanSansSC-VF.woff2",
    "SOURCE-HAN-SANS-LICENSE.txt",
}


class ProjectContractTests(unittest.TestCase):
    def test_root_contains_launcher_and_readme(self):
        root_files = {
            item.name
            for item in TOOL_ROOT.iterdir()
            if item.is_file() and item.name != '.DS_Store'  # Finder metadata is not a distributed tool file.
        }
        self.assertEqual(root_files, {"启动塔占比修改及增加Btile工具.command", "README.md"})
        for directory in ("frontend", "scripts", "docs", "运行数据", "输出模板"):
            with self.subTest(directory=directory):
                self.assertTrue((TOOL_ROOT / directory).is_dir())
        for directory in ("运行数据", "输出模板"):
            with self.subTest(ignore=directory):
                self.assertTrue((TOOL_ROOT / directory / ".gitignore").is_file())

    def test_local_fonts_have_licenses(self):
        fonts = ASSETS / "fonts"
        self.assertEqual({item.name for item in fonts.iterdir()}, FONT_FILES)
        for name in FONT_FILES:
            with self.subTest(font=name):
                self.assertGreater((fonts / name).stat().st_size, 0)

    def test_css_uses_one_font_stack_and_five_size_scale(self):
        css = (ASSETS / "styles.css").read_text(encoding="utf-8")
        font_families = re.findall(r"(?<![-\w])font-family\s*:\s*([^;]+);", css)
        self.assertEqual(
            font_families.count('"Inter Local", "Source Han Sans SC Local", sans-serif'),
            1,
        )
        self.assertTrue(
            all(
                family in (
                    '"Inter Local", "Source Han Sans SC Local", sans-serif',
                    '"Inter Local"',
                    '"Source Han Sans SC Local"',
                    "inherit",
                )
                for family in font_families
            )
        )
        sizes = set(re.findall(r"(?<![-\w])font-size\s*:\s*([^;]+);", css))
        self.assertEqual(sizes, {"30px", "24px", "18px", "14px", "12px"})
        self.assertIn("font-variant-numeric: tabular-nums", css)
        self.assertRegex(css, r"code\s*,\s*pre\s*,\s*kbd\s*,\s*samp\s*\{[^}]*font-family:\s*inherit;")
        self.assertNotRegex(css, r"(?i)clamp\s*\(|monospace|font-size\s*:\s*0(?:px|em|rem|%)?\s*;")

    def test_css_uses_local_fonts_and_olive_accent(self):
        css = (ASSETS / "styles.css").read_text(encoding="utf-8")
        self.assertIn("/assets/fonts/InterVariable.woff2", css)
        self.assertIn("/assets/fonts/SourceHanSansSC-VF.woff2", css)
        self.assertRegex(css, r"--accent:\s*#[0-9a-fA-F]{6};")
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn("overflow-x: auto", css)

    def test_homepage_declares_local_workbench(self):
        html = (TOOL_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        for text in ("塔占比修改及增加Btile工具", "优化工作台", "运行记录", "设置", "导入模板", "配置参数", "导出模板"):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertIn('href="/assets/styles.css"', html)
        self.assertIn('src="/assets/app.js"', html)


if __name__ == "__main__":
    unittest.main()
