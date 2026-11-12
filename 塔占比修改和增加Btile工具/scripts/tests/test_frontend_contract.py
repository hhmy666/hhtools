"""Static product, accessibility, and visual contracts for the three-step UI.

These tests intentionally use only Python's standard library.  They validate
durable user-facing contracts without coupling to exact wrapper counts or CSS
class names beyond the few canonical landmarks documented in UX-CONTRACT.md.
"""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional


TOOL_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = TOOL_ROOT / "frontend"
INDEX = FRONTEND / "index.html"
STYLES = FRONTEND / "assets" / "styles.css"
SCRIPT = FRONTEND / "assets" / "app.js"
VIEW_MODEL = FRONTEND / "assets" / "view-model.js"
HELP_CONTENT = FRONTEND / "assets" / "help-content.js"
ICONS = FRONTEND / "assets" / "icons.svg"
DOCS = TOOL_ROOT / "docs"


class Node:
    """Small DOM-like node sufficient for source-level semantic assertions."""

    def __init__(
        self,
        tag: str,
        attrs: Optional[Iterable[tuple]] = None,
        parent: Optional["Node"] = None,
    ) -> None:
        self.tag = tag
        self.attrs: Dict[str, Optional[str]] = dict(attrs or [])
        self.parent = parent
        self.children: List["Node"] = []
        self.data: List[str] = []

    @property
    def classes(self) -> set:
        return set((self.attrs.get("class") or "").split())

    @property
    def text(self) -> str:
        pieces = list(self.data)
        pieces.extend(child.text for child in self.children)
        return re.sub(r"\s+", " ", " ".join(pieces)).strip()

    def descendants(self, predicate: Callable[["Node"], bool]) -> List["Node"]:
        found: List[Node] = []
        for child in self.children:
            if predicate(child):
                found.append(child)
            found.extend(child.descendants(predicate))
        return found


class SourceDOM(HTMLParser):
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: List[tuple]) -> None:
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].data.append(data)

    def all(self, tag: Optional[str] = None) -> List[Node]:
        return self.root.descendants(lambda node: tag is None or node.tag == tag)

    def by_id(self, node_id: str) -> Optional[Node]:
        return next((node for node in self.all() if node.attrs.get("id") == node_id), None)

    def by_attr(self, name: str, value: Optional[str] = None) -> List[Node]:
        return [
            node
            for node in self.all()
            if name in node.attrs and (value is None or node.attrs.get(name) == value)
        ]


def media_block(css: str, query: str) -> str:
    """Collect every balanced matching body, as the CSS cascade does.

    Feature-local responsive rules can precede the app-shell rules; the first
    media body alone is not the effective narrow-screen contract.
    """
    pattern = r"@media\s*\(\s*max-width\s*:\s*" + re.escape(query) + r"\s*\)\s*\{"
    bodies = []
    for match in re.finditer(pattern, css):
        opening = css.find("{", match.start())
        depth = 0
        for index in range(opening, len(css)):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(css[opening + 1:index])
                    break
    return "\n".join(bodies)


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLES.read_text(encoding="utf-8")
        cls.js = SCRIPT.read_text(encoding="utf-8")
        cls.view_model_js = VIEW_MODEL.read_text(encoding="utf-8")
        cls.help_js = HELP_CONTENT.read_text(encoding="utf-8")
        bulk_js = (FRONTEND / "assets" / "batch-import.js").read_text(encoding="utf-8")
        cls.runtime_js = "\n".join((cls.view_model_js, cls.js, bulk_js))
        cls.dom = SourceDOM(cls.html)
        cls.source = "\n".join((cls.html, cls.runtime_js, cls.help_js))

    def assertContainsAll(self, haystack: str, needles: Iterable[str]) -> None:
        for needle in needles:
            with self.subTest(text=needle):
                self.assertTrue(needle in haystack, f"缺少契约内容：{needle}")

    def test_three_step_flow_has_chinese_titles_and_one_page_heading(self):
        flows = {
            node.attrs.get("data-flow"): node
            for node in self.dom.by_attr("data-flow")
            if node.tag == "section"
        }
        self.assertEqual(set(flows), {"1", "2", "3"})
        expected_titles = {
            "1": "导入关卡模板",
            "2": "配置塔保留与补牌规则",
            "3": "验收并导出新模板",
        }
        for step, expected in expected_titles.items():
            with self.subTest(step=step):
                flow = flows[step]
                label_id = flow.attrs.get("aria-labelledby")
                self.assertTrue(label_id, "每个流程页必须用 aria-labelledby 指向唯一主标题")
                title = self.dom.by_id(label_id or "")
                self.assertIsNotNone(title)
                self.assertIn(title.tag, {"h1", "h2"})
                self.assertEqual(title.text, expected)
                page_headings = flow.descendants(lambda node: node.tag == "h1")
                self.assertEqual(
                    len(page_headings),
                    1,
                    "同一步只能有一个页面级 h1；卡片标题使用 h2 并保持卡片字号",
                )

        stepper = next(
            (node for node in self.dom.all("ol") if "流程" in (node.attrs.get("aria-label") or "")),
            None,
        )
        self.assertIsNotNone(stepper)
        step_items = stepper.descendants(lambda node: node.tag == "li")[:3]
        self.assertEqual(len(step_items), 3)
        for index, (item, label) in enumerate(
            zip(step_items, ("导入模板", "配置参数", "导出模板")),
            start=1,
        ):
            with self.subTest(step=index):
                self.assertIn(str(index), item.text)
                self.assertIn(label, item.text)

    def test_global_shell_navigation_and_topbar_copy(self):
        self.assertContainsAll(
            self.dom.root.text,
            (
                "塔占比修改及增加Btile工具",
                "优化工作台",
                "运行记录",
                "设置",
                "正在连接本地服务",
                "重新开始",
                "使用说明",
            ),
        )
        navs = self.dom.all("nav")
        self.assertTrue(any("优化工作台" in nav.text and "设置" in nav.text for nav in navs))
        self.assertTrue(self.dom.by_attr("aria-current"), "当前导航或步骤必须暴露 aria-current")
        self.assertIsNotNone(self.dom.by_id("service-status"))

        for button_id, accessible_name in (
            ("restart-button", "重新开始"),
            ("help-button", "使用说明"),
        ):
            with self.subTest(button=button_id):
                button = self.dom.by_id(button_id)
                self.assertIsNotNone(button)
                self.assertEqual(
                    button.attrs.get("aria-label"),
                    accessible_name,
                    "移动端隐藏文字后，顶部图标按钮仍须保留稳定可访问名称",
                )

    def test_mobile_stepper_does_not_create_page_level_horizontal_scroll(self):
        mobile = "\n".join((media_block(self.css, "760px"), media_block(self.css, "480px")))
        self.assertRegex(
            mobile,
            r"\.flow-rail\s*\{[^}]*overflow-x\s*:\s*(?:visible|hidden)",
            "移动端三步流程不应显示自身横向滚动条",
        )
        self.assertRegex(
            mobile,
            r"\.flow-rail\s+li\s*\{[^}]*min-width\s*:\s*0",
            "移动端流程项应允许收缩到三等分宽度",
        )

    def test_first_run_binding_and_portable_path_confirmation(self):
        binding_dialogs = [
            dialog
            for dialog in self.dom.all("dialog")
            if "确认" in dialog.text and "路径" in dialog.text
        ]
        self.assertTrue(binding_dialogs, "首次启动必须有原生路径确认 dialog")
        dialog_text = " ".join(dialog.text for dialog in binding_dialogs)
        self.assertContainsAll(dialog_text, ("确认使用", "修改路径", "模板仓", "Tile 仓"))
        self.assertContainsAll(
            self.source,
            (
                "tile/Tile/Assets/Resources/TileMatchLevelData",
                "tileleveltemplate/Template",
                "重新发现",
                "已确认",
            ),
        )

    def test_import_configuration_export_history_and_settings_copy(self):
        self.assertContainsAll(
            self.source,
            (
                "_m.bytes",
                "源文件只读",
                "输入模板 ID",
                "上传模板文件",
                "解析模板",
                "塔 Tile 占比上限",
                "单层非塔 Tile 建议上限",
                "最多新增层数",
                "开始生成",
                "预览导出",
                "下载全部已验收模板 ZIP",
                "预览同步",
                "最后更新时间",
                "无需优化",
                "默认命名规则",
                "默认导出目录",
            ),
        )
        for view_name in ("history", "settings"):
            with self.subTest(view=view_name):
                self.assertTrue(
                    self.dom.by_attr("data-view", view_name),
                    f"{view_name} 必须是可导航的独立页面视图",
                )

    def test_import_results_expose_complete_inspection_and_blocking_issues(self):
        self.assertContainsAll(
            self.runtime_js,
            (
                "describeImportInspection",
                "hasBlockingIssues",
                "inspection-facts",
                "inspection-issues",
                "结构异常",
                "可继续·有提醒",
            ),
        )

    def test_import_modes_share_a_single_centered_card_and_two_operation_entries(self):
        flow = next(node for node in self.dom.by_attr("data-flow", "1") if node.tag == "section")
        cards = flow.descendants(lambda node: "import-card" in node.classes)
        self.assertEqual(len(cards), 1)
        self.assertFalse(flow.descendants(lambda node: node.tag == "aside"), "导入页不再堆叠说明侧栏")
        self.assertEqual(len(flow.descendants(lambda node: node.tag == "form")), 1)
        card = cards[0]
        tabs = card.descendants(lambda node: node.attrs.get("role") == "tab")
        self.assertEqual(len(tabs), 2)
        self.assertEqual({node.text for node in tabs}, {"输入模板 ID", "上传模板文件"})
        for tab in tabs:
            panel = self.dom.by_id(tab.attrs.get("aria-controls") or "")
            self.assertIsNotNone(panel)
            self.assertEqual(panel.attrs.get("role"), "tabpanel")
            self.assertEqual(panel.attrs.get("aria-labelledby"), tab.attrs.get("id"))
        self.assertEqual(sum(tab.attrs.get("aria-selected") == "true" for tab in tabs), 1)
        self.assertIsNotNone(self.dom.by_id("template-ids"))
        self.assertIsNotNone(self.dom.by_id("template-files"))
        source = self.dom.by_id("source-repository")
        self.assertIsNotNone(source)
        self.assertEqual({node.attrs.get("value") for node in source.children}, {"tile", "template"})
        entries = card.descendants(lambda node: "data-import-workspace-mode" in node.attrs and "hidden" not in node.attrs)
        self.assertEqual([node.text for node in entries], ["去增加塔", "去减少塔"])
        self.assertEqual([node.attrs.get("data-import-workspace-mode") for node in entries],
                         ["increase", "reduce"])
        self.assertTrue(all(node.attrs.get("type") == "submit" for node in entries))
        ordinary = self.dom.by_id("inspect-ordinary-button")
        self.assertEqual(ordinary.attrs.get("data-import-workspace-mode"), "ordinary_add")
        self.assertIn("hidden", ordinary.attrs)
        self.assertFalse(self.dom.by_attr("data-workspace-mode"),
                         "流程条上方不再重复放置模式开关")
        self.assertRegex(self.css, re.compile(r"\.import-card\s*\{[^}]*margin\s*:[^;]*auto", re.S))
        self.assertContainsAll(self.runtime_js, ("parseTemplateIds", "template_ids:", "source_repository:"))

    def test_configuration_uses_template_matrix_and_one_parameter_editor(self):
        flow = next(node for node in self.dom.by_attr("data-flow", "2") if node.tag == "section")
        matrices = flow.descendants(lambda node: "configuration-table" in node.classes)
        editors = flow.descendants(lambda node: "parameter-editor" in node.classes)
        self.assertEqual(len(matrices), 1)
        self.assertEqual(len(editors), 1, "单个参数编辑器承载整批或当前模板，避免逐模板表单堆叠")
        headers = matrices[0].descendants(lambda node: node.tag == "th")
        self.assertEqual([node.text for node in headers], ["模板", "原塔占比", "当前方案", "操作"])
        self.assertIsNotNone(self.dom.by_id("edit-batch-parameters"))
        self.assertIsNotNone(self.dom.by_id("restore-batch-parameters"))
        self.assertIsNotNone(self.dom.by_id("parameter-override-editor"))
        self.assertIsNotNone(self.dom.by_id("batch-summary-text"))

    def test_result_identifies_source_output_and_keeps_logs_collapsed(self):
        table_body = self.dom.by_id("result-table-body")
        self.assertIsNotNone(table_body)
        table = table_body.parent
        self.assertEqual(table.tag, "table")
        headers = table.descendants(lambda node: node.tag == "th")
        self.assertContainsAll(" ".join(node.text for node in headers), ("原模板 ID", "新模板 ID", "塔占比变化", "挪动 Tile", "验收状态"))
        log = self.dom.by_id("log-list")
        self.assertIsNotNone(log)
        parent = log.parent
        while parent is not None and parent.tag != "details":
            parent = parent.parent
        self.assertIsNotNone(parent, "日志和过程只在按需展开区呈现")
        self.assertNotIn("open", parent.attrs)
        self.assertTrue(parent.descendants(lambda node: node.tag == "summary"))

    def test_help_covers_new_parameter_task_and_keeps_existing_continuous_reading_panes(self):
        help_dialog = self.dom.by_id("help-dialog")
        self.assertIsNotNone(help_dialog)
        topic_ids = re.findall(r'^\s+id:\s*"([^"]+)"', self.help_js, re.M)
        self.assertEqual(set(topic_ids), {"start", "import", "config", "generate", "export", "sync", "parameters", "settings", "faq", "ordinary_add"})
        self.assertIn('/assets/help-content.js', self.html)
        self.assertIsNotNone(self.dom.by_id("help-search-input"))
        self.assertIsNotNone(self.dom.by_id("help-content"))
        self.assertIsNotNone(self.dom.by_id("help-navigation"))
        self.assertContainsAll(self.js, ("initializeHelp", "openHelp"))
        self.assertContainsAll(
            self.help_js,
            (
                "怎么操作",
                "完成检查",
                "参数选择依据",
                'class="help-table"',
                'class="help-steps"',
                '<details class="help-details">',
                "严格小于",
                "Git",
            ),
        )
        self.assertRegex(self.help_js, r"0[^<\n]{0,30}(?:清空塔|全部移出)")
        self.assertRegex(self.css, re.compile(r"\.help-dialog\s*\{[^}]*overflow\s*:\s*hidden", re.S))
        self.assertRegex(self.css, re.compile(r"\.help-shell\s*\{[^}]*grid-template-rows\s*:\s*auto\s+minmax\(0,\s*1fr\)", re.S))
        self.assertRegex(self.css, re.compile(r"\.help-content\s*\{[^}]*overflow(?:-y)?\s*:\s*auto", re.S))

    def test_forms_are_real_labeled_and_use_application_validation(self):
        forms = self.dom.all("form")
        self.assertGreaterEqual(len(forms), 3, "导入、参数和导出/设置应使用真实 form")
        for form in forms:
            with self.subTest(form=form.attrs.get("id", form.text[:30])):
                self.assertIn("novalidate", form.attrs)

        labels_for = {
            label.attrs.get("for")
            for label in self.dom.all("label")
            if label.attrs.get("for")
        }
        controls = [
            node
            for node in self.dom.all()
            if node.tag in {"input", "select", "textarea"}
            and node.attrs.get("type", "").lower() != "hidden"
        ]
        self.assertTrue(controls)
        for control in controls:
            control_id = control.attrs.get("id")
            nested_label = control.parent
            while nested_label and nested_label.tag != "label":
                nested_label = nested_label.parent
            with self.subTest(control=control_id or control.tag):
                self.assertTrue(control_id, "静态表单控件必须有稳定 id")
                self.assertTrue(
                    control_id in labels_for or nested_label is not None,
                    f"{control_id} 必须有真实 label，而不是只靠 placeholder/aria-label",
                )
                described_by = control.attrs.get("aria-describedby")
                if described_by:
                    for reference in described_by.split():
                        self.assertIsNotNone(self.dom.by_id(reference))

        self.assertNotRegex(self.js, r"\breportValidity\s*\(")
        self.assertRegex(self.js, r"(?:first|invalid|error)[\w-]*[^\n]{0,160}\.focus\s*\(")

    def test_dialog_status_progress_and_focus_contract(self):
        dialogs = self.dom.all("dialog")
        self.assertGreaterEqual(
            len(dialogs),
            4,
            "至少需要首次绑定、使用说明、重新开始和同步冲突四类 dialog",
        )
        for dialog in dialogs:
            with self.subTest(dialog=dialog.attrs.get("id")):
                labelled_by = dialog.attrs.get("aria-labelledby")
                described_by = dialog.attrs.get("aria-describedby")
                self.assertTrue(labelled_by)
                self.assertTrue(described_by)
                self.assertIsNotNone(self.dom.by_id(labelled_by or ""))
                self.assertIsNotNone(self.dom.by_id(described_by or ""))

        service = self.dom.by_id("service-status")
        self.assertIsNotNone(service)
        self.assertEqual(service.attrs.get("role"), "status")
        self.assertTrue(service.descendants(lambda node: node.tag == "svg"))
        self.assertTrue(service.text)

        progresses = self.dom.all("progress")
        self.assertTrue(progresses, "后台任务必须有真实 progress")
        self.assertTrue(
            all(item.attrs.get("aria-label") or item.attrs.get("aria-labelledby") for item in progresses)
        )
        self.assertContainsAll(
            self.source,
            (
                "读取源文件",
                "检查模板",
                "计算塔分布",
                "搜索补牌位置",
                "重写并独立验收",
            ),
        )
        self.assertIn("showModal", self.js)
        self.assertIn("document.activeElement", self.js)
        self.assertRegex(self.js, r"(?:\.inert\s*=|setAttribute\(\s*[\"']inert)")
        self.assertRegex(self.js, r"\.focus\s*\(")

    def test_four_strategies_advanced_towers_and_override_controls(self):
        option_values = {
            option.attrs.get("value")
            for option in self.dom.all("option")
        }
        self.assertTrue(
            {"top_first", "bottom_first", "balanced", "seeded_random"}.issubset(option_values)
        )
        self.assertContainsAll(
            self.source,
            (
                "顶层优先",
                "底层优先",
                "五五开",
                "随机",
                "随机种子",
                "批次默认参数",
                "使用批次默认",
                "单独设置",
                "恢复批次默认",
                "高级逐塔",
                "逐塔保留数量",
            ),
        )

    def test_naming_and_export_controls_only_appear_in_settings_or_step_three(self):
        flows = {
            node.attrs.get("data-flow"): node
            for node in self.dom.by_attr("data-flow")
            if node.tag == "section"
        }
        self.assertEqual(set(flows), {"1", "2", "3"})
        forbidden_in_configuration = ("命名规则", "导出目录", "同步路径", "lessT{原模板ID}")
        for phrase in forbidden_in_configuration:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, flows["2"].text)

        settings_view = next(iter(self.dom.by_attr("data-view", "settings")), None)
        self.assertIsNotNone(settings_view)
        for scope_name, scope in (("第三步", flows["3"]), ("设置", settings_view)):
            with self.subTest(scope=scope_name):
                self.assertContainsAll(scope.text, ("命名规则", "导出目录"))
        self.assertIn("lessT{原模板ID}", self.source)

    def test_no_tower_partial_failure_rerun_and_conflict_recovery_states(self):
        self.assertContainsAll(
            self.source,
            (
                "无需优化",
                "不生成新文件",
                "部分成功",
                "单独调整并重新运行",
                "内容相同，已存在",
                "内容冲突",
                "备份并替换",
                "计划已过期",
                "重新预览同步",
                "已回滚",
                "回滚失败，需要人工处理",
                "工具未执行 Git 操作",
                "跳过此冲突",
                "单文件下载",
            ),
        )
        self.assertContainsAll(self.source, ("个通过", "个失败", "个无需优化"))

    def test_final_api_shape_and_async_safety_contracts(self):
        target = self.dom.by_id("target-ratio")
        self.assertIsNotNone(target)
        self.assertEqual(target.attrs.get("max"), "100")
        self.assertContainsAll(
            self.runtime_js,
            (
                "TowerReducerViewModel",
                "plan_summary",
                "verification",
                'status === "no_tower"',
                '"decode"',
                '"distribution"',
                '"placement"',
                '"register_output"',
                "AbortController",
                "batchController",
                "abortableDelay",
                "requestGeneration",
                "invalidatePlans",
                "safeStorage",
                "document.title",
            ),
        )
        self.assertNotIn('action: "rename_no_op"', self.runtime_js)
        self.assertIsNotNone(self.dom.by_id("rerun-dialog"))
        self.assertIsNotNone(self.dom.by_id("path-candidate-list"))
        self.assertIsNotNone(self.dom.by_id("help-navigation"))
        self.assertRegex(self.runtime_js, r"Math\.min\([^\n]*(?:8000|8_000)")
        self.assertContainsAll(
            self.runtime_js,
            (
                "shouldAcceptJobResponse",
                "validateRetainedCounts",
                "normalizeSyncPlanEntries",
                "interpretSyncReport",
                "summarizeTemplateOutcomes",
                "normalizeHistoryRows",
            ),
        )

    def test_sync_commit_preserves_truthful_partial_and_recovery_states(self):
        self.assertContainsAll(
            self.js,
            (
                "outcome.confirmationRequired",
                "outcome.auditFailed",
                "outcome.recoveryRequired",
                "outcome.allWriteAttemptsRolledBack",
                "outcome.hasMixedCommittedAndRolledBack",
                "审计记录未落盘",
                "保留当前预览和确认令牌",
            ),
        )
        self.assertIn("file.hasInspection", self.js)
        self.assertNotIn("const rolledBack = asText(result.status)", self.js)

    def test_history_and_noop_copy_match_current_api_semantics(self):
        history = self.dom.by_id("history-view")
        self.assertIsNotNone(history)
        self.assertIn("最后更新时间", history.text)
        self.assertNotIn("创建时间", history.text)

        readme = (DOCS / "README.md").read_text(encoding="utf-8")
        algorithm = (DOCS / "算法与验收口径.md").read_text(encoding="utf-8")
        self.assertNotIn("尝试数、产物数", readme)
        self.assertNotIn("模板无塔或已满足目标", self.source)
        self.assertNotIn("无塔模板或已经严格低于目标的模板会标记", algorithm)

    def test_dynamic_statuses_keep_icons_and_busy_semantics(self):
        self.assertIn("function setStatusPill", self.js)
        for element_id in (
            "batch-status",
            "export-status",
            "settings-binding-status",
        ):
            with self.subTest(element_id=element_id):
                self.assertRegex(
                    self.js,
                    rf'setStatusPill\(byId\("{re.escape(element_id)}"\)',
                )
        self.assertIn('setAttribute("aria-busy", "true")', self.js)
        self.assertIn('removeAttribute("aria-busy")', self.js)

    def test_runtime_view_model_and_docs_describe_real_generation_contract(self):
        self.assertIn('/assets/view-model.js', self.html)
        self.assertContainsAll(
            self.source,
            (
                "开始生成",
                "跳过此冲突",
                "备份并替换",
                "单文件下载",
                "多个候选不会自动选择",
            ),
        )
        for document in (DOCS / "README.md", DOCS / "算法与验收口径.md"):
            content = document.read_text(encoding="utf-8")
            self.assertNotIn("生成优化计划", content)
            self.assertContainsAll(content, ("开始生成", "导出", "同步"))

    def test_supporting_ui_states_have_layout_and_accessibility_styles(self):
        self.assertContainsAll(
            self.css,
            (
                ".help-shell",
                ".help-body",
                ".help-navigation",
                ".candidate-list",
                ".recommendation-box",
                ".field-error",
                ".row-actions",
                ".result-evidence",
                ".status-pill svg",
            ),
        )
        self.assertRegex(self.css, re.compile(r"\.phase-list\s*\{[^}]*repeat\(5,", re.S))
        self.assertNotRegex(
            self.css,
            re.compile(r"\*\s*,\s*\*::before\s*,\s*\*::after\s*\{[^}]*\.01ms[^}]*!important", re.S),
        )

    def test_workspace_has_no_persistent_duplicate_page_title(self):
        workspace = self.dom.by_id("workspace-view")
        self.assertIsNotNone(workspace)
        self.assertFalse(
            [node for node in workspace.children if node.tag in {"h1", "h2"} or "page-heading" in node.classes],
            "工作台只显示当前流程页标题，不保留额外的介绍标题区",
        )

    def test_typography_olive_tokens_and_local_assets_are_strict(self):
        exact_stack = '"Inter Local", "Source Han Sans SC Local", sans-serif'
        declarations = re.findall(r"(?<![-\w])font-family\s*:\s*([^;]+);", self.css)
        self.assertEqual(declarations.count(exact_stack), 1)
        self.assertTrue(
            all(value in {exact_stack, '"Inter Local"', '"Source Han Sans SC Local"', "inherit"} for value in declarations)
        )

        self.assertTrue(ICONS.is_file(), "缺少本地 SVG 图标精灵 frontend/assets/icons.svg")
        icons_source = ICONS.read_text(encoding="utf-8")
        all_frontend_source = "\n".join((self.html, self.css, self.runtime_js, self.help_js, icons_source))
        sizes = {
            value.strip()
            for value in re.findall(r"(?<![-\w])font-size\s*:\s*([^;\"']+)", all_frontend_source)
        }
        self.assertEqual(sizes, {"30px", "24px", "18px", "14px", "12px"})
        self.assertNotRegex(
            all_frontend_source,
            r"(?i)clamp\s*\(|monospace|font-size\s*:\s*0(?:px|em|rem|%)?\s*(?:;|[\"'])",
        )
        self.assertNotRegex(
            icons_source,
            r"font-size\s*=\s*[\"'](?!(?:12|14|18|24|30)(?:px)?[\"'])",
        )
        self.assertContainsAll(
            self.css.lower(),
            (
                "/assets/fonts/intervariable.woff2",
                "/assets/fonts/sourcehansanssc-vf.woff2",
                "#68743a",
                "#4d5927",
                "#f1f3e8",
                "#f8f9f5",
                "font-variant-numeric: tabular-nums",
            ),
        )
        self.assertNotRegex(self.css, r"https?://")
        self.assertRegex(self.css, r"textarea[^{}]*\{[^}]*resize\s*:\s*none")
        self.assertIn(":focus-visible", self.css)

    def test_responsive_contract_keeps_navigation_and_actions_reachable(self):
        narrow = media_block(self.css, "760px")
        phone = media_block(self.css, "480px")
        self.assertTrue(narrow, "缺少 760px 响应式规则")
        self.assertTrue(phone, "缺少 480px 响应式规则")

        sidebar_hidden = bool(re.search(r"\.sidebar\s*\{[^}]*display\s*:\s*none", narrow, re.S))
        alternate_nav = bool(re.search(r"\.(?:mobile|bottom)[-_]nav", narrow, re.I))
        fixed_sidebar = bool(
            re.search(r"\.sidebar\s*\{[^}]*(?:position\s*:\s*fixed)[^}]*bottom\s*:", narrow, re.S)
        )
        self.assertTrue(
            fixed_sidebar or (sidebar_hidden and alternate_nav),
            "760px 下不能只隐藏主导航；必须转成可达的底部/移动导航",
        )
        if alternate_nav:
            self.assertTrue(
                any(
                    any(re.search(r"(?:mobile|bottom)[-_]nav", class_name, re.I) for class_name in node.classes)
                    for node in self.dom.all("nav")
                ),
                "CSS 中的窄屏导航必须有对应语义 nav",
            )
        self.assertRegex(narrow, r"grid-template-columns\s*:[^;]*1fr|display\s*:\s*block")
        self.assertRegex(phone, r"width\s*:\s*100%")
        self.assertIn("overflow-x: auto", self.css)
        self.assertRegex(phone, r"dialog|\.dialog")

    def test_no_meaningless_english_eyebrows_or_browser_native_dialogs(self):
        for phrase in (
            "STEP 01",
            "LOCAL WORKFLOW",
            "LOCAL SOURCES",
            "LATEST OUTPUT",
            "PLAYER REPLAY",
            "AI POWERED",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.source.upper())

        for node in self.dom.all():
            if node.classes.intersection({"eyebrow", "section-kicker", "page-kicker"}) and node.text:
                with self.subTest(kicker=node.text):
                    self.assertRegex(node.text, r"[\u4e00-\u9fff]", "眉题必须是有业务意义的中文")

        self.assertNotRegex(self.html, r"\son[a-z]+\s*=")
        self.assertNotRegex(self.js, r"\b(?:window\.)?(?:alert|confirm|prompt)\s*\(")

    def test_static_delivery_docs_cover_usage_algorithm_and_resources(self):
        expected = {
            "README.md": ("输入模板 ID", "上传模板文件", "参数选择依据", "命名", "导出", "同步", "怎么操作", "完成检查"),
            "算法与验收口径.md": ("塔 Tile 占比", "对称", "堆叠", "独立验收"),
            "第三方资源说明.md": ("Inter", "Source Han Sans SC", "许可证"),
        }
        for filename, phrases in expected.items():
            with self.subTest(document=filename):
                path = DOCS / filename
                self.assertTrue(path.is_file(), f"缺少 {filename}")
                content = path.read_text(encoding="utf-8")
                self.assertContainsAll(content, phrases)


if __name__ == "__main__":
    unittest.main()
