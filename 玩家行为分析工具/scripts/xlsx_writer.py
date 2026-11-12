"""Small, dependency-free XLSX writer for the player-behaviour tool.

The workbooks produced here contain static values only.  They intentionally do
not depend on openpyxl so that the local tool can export results in a clean
Python environment after the pandas/numpy analysis has finished.
"""

from __future__ import annotations

import math
import numbers
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.etree.ElementTree import fromstring
from xml.sax.saxutils import escape


PALETTE = {
    "ink": "17333A",
    "muted": "607379",
    "navy": "1F4E5F",
    "blue": "3C6F9A",
    "blue_light": "E6EEF6",
    "teal": "2A7887",
    "teal_light": "DDEFF1",
    "green": "4D7B61",
    "green_light": "E5F0E8",
    "orange": "C97142",
    "orange_light": "F8E8DE",
    "red": "B44A4A",
    "red_light": "F7E0E0",
    "yellow": "D8AE38",
    "yellow_light": "FAF1D4",
    "grey": "718086",
    "grey_light": "EFF3F4",
    "white": "FFFFFF",
}


STYLE_NORMAL = 0
STYLE_HEADER = 1
STYLE_TITLE = 2
STYLE_SUBTITLE = 3
STYLE_SECTION = 4
STYLE_WRAP = 5
STYLE_INTEGER = 6
STYLE_PERCENT = 7
STYLE_DECIMAL = 8
STYLE_BLUE_CELL = 9
STYLE_WARNING = 10
STYLE_DANGER = 11
STYLE_SUCCESS = 12
STYLE_TAG_BASE = 20  # T0..T7 use 20..27.
STYLE_LAYER_BASE = 30  # L4,L3,L2,L1,L0,P use 30..35.


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return "".join(
        char
        for char in text
        if char in "\t\n\r" or ord(char) >= 32
    )


def _xml(value: Any) -> str:
    return escape(_clean_text(value), {'"': "&quot;"})


def _excel_column(index: int) -> str:
    value = index + 1
    output = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _cell_ref(row: int, column: int) -> str:
    return f"{_excel_column(column)}{row + 1}"


def _safe_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        return list(value.to_dict(orient="records"))
    return [dict(row) for row in value]


@dataclass
class Cell:
    value: Any = None
    style: int = STYLE_NORMAL


@dataclass
class TableSpec:
    name: str
    start_row: int
    start_col: int
    end_row: int
    end_col: int
    headers: list[str]


@dataclass
class Sheet:
    name: str
    tab_color: str = PALETTE["teal"]
    gridlines: bool = False
    rows: list[list[Cell]] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)
    column_widths: dict[int, float] = field(default_factory=dict)
    row_heights: dict[int, float] = field(default_factory=dict)
    freeze_rows: int = 0
    freeze_columns: int = 0
    auto_filter: str | None = None
    tables: list[TableSpec] = field(default_factory=list)

    def ensure(self, row: int, column: int) -> None:
        while len(self.rows) <= row:
            self.rows.append([])
        while len(self.rows[row]) <= column:
            self.rows[row].append(Cell())

    def set(self, row: int, column: int, value: Any, style: int = STYLE_NORMAL) -> None:
        self.ensure(row, column)
        self.rows[row][column] = Cell(value, style)

    def write_row(
        self, row: int, values: Sequence[Any], styles: Sequence[int] | int = STYLE_NORMAL
    ) -> None:
        if isinstance(styles, int):
            styles = [styles] * len(values)
        for column, (value, style) in enumerate(zip(values, styles)):
            self.set(row, column, value, style)

    def add_table(
        self,
        name: str,
        header_row: int,
        start_col: int,
        row_count: int,
        headers: Sequence[str],
    ) -> None:
        end_row = header_row + max(0, row_count)
        end_col = start_col + len(headers) - 1
        self.tables.append(
            TableSpec(name, header_row, start_col, end_row, end_col, list(headers))
        )
        self.auto_filter = f"{_cell_ref(header_row, start_col)}:{_cell_ref(end_row, end_col)}"


class Workbook:
    def __init__(self) -> None:
        self.sheets: list[Sheet] = []

    def add_sheet(self, name: str, tab_color: str = PALETTE["teal"]) -> Sheet:
        if any(sheet.name == name for sheet in self.sheets):
            raise ValueError(f"Duplicate sheet name: {name}")
        sheet = Sheet(name=name[:31], tab_color=tab_color)
        self.sheets.append(sheet)
        return sheet

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        table_ids: dict[tuple[int, int], int] = {}
        next_table_id = 1
        for sheet_index, sheet in enumerate(self.sheets, start=1):
            for local_index, _table in enumerate(sheet.tables, start=1):
                table_ids[(sheet_index, local_index)] = next_table_id
                next_table_id += 1

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", self._content_types(next_table_id - 1))
            archive.writestr("_rels/.rels", self._root_rels())
            archive.writestr("docProps/app.xml", self._app_xml())
            archive.writestr("docProps/core.xml", self._core_xml())
            archive.writestr("xl/workbook.xml", self._workbook_xml())
            archive.writestr("xl/_rels/workbook.xml.rels", self._workbook_rels())
            archive.writestr("xl/styles.xml", _styles_xml())
            for sheet_index, sheet in enumerate(self.sheets, start=1):
                archive.writestr(
                    f"xl/worksheets/sheet{sheet_index}.xml",
                    self._sheet_xml(sheet, sheet_index, table_ids),
                )
                if sheet.tables:
                    archive.writestr(
                        f"xl/worksheets/_rels/sheet{sheet_index}.xml.rels",
                        self._sheet_rels(sheet, sheet_index, table_ids),
                    )
                for local_index, table in enumerate(sheet.tables, start=1):
                    table_id = table_ids[(sheet_index, local_index)]
                    archive.writestr(
                        f"xl/tables/table{table_id}.xml",
                        self._table_xml(table, table_id),
                    )
        return target

    def _content_types(self, table_count: int) -> str:
        overrides = [
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
        ]
        overrides.extend(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(self.sheets) + 1)
        )
        overrides.extend(
            f'<Override PartName="/xl/tables/table{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>'
            for index in range(1, table_count + 1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            + "".join(overrides)
            + "</Types>"
        )

    @staticmethod
    def _root_rels() -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            "</Relationships>"
        )

    def _app_xml(self) -> str:
        titles = "".join(f"<vt:lpstr>{_xml(sheet.name)}</vt:lpstr>" for sheet in self.sheets)
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            '<Application>玩家行为分析工具</Application><AppVersion>1.0</AppVersion>'
            f'<TitlesOfParts><vt:vector size="{len(self.sheets)}" baseType="lpstr">{titles}</vt:vector></TitlesOfParts>'
            "</Properties>"
        )

    @staticmethod
    def _core_xml() -> str:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:creator>玩家行为分析工具</dc:creator><cp:lastModifiedBy>玩家行为分析工具</cp:lastModifiedBy>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
            "</cp:coreProperties>"
        )

    def _workbook_xml(self) -> str:
        sheets = "".join(
            f'<sheet name="{_xml(sheet.name)}" sheetId="{index}" r:id="rId{index}"/>'
            for index, sheet in enumerate(self.sheets, start=1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheets}</sheets><calcPr calcId=\"191029\" fullCalcOnLoad=\"1\"/></workbook>"
        )

    def _workbook_rels(self) -> str:
        relationships = [
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(self.sheets) + 1)
        ]
        relationships.append(
            f'<Relationship Id="rId{len(self.sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(relationships)
            + "</Relationships>"
        )

    def _sheet_xml(
        self, sheet: Sheet, sheet_index: int, table_ids: Mapping[tuple[int, int], int]
    ) -> str:
        max_row = max(1, len(sheet.rows))
        max_col = max(1, max((len(row) for row in sheet.rows), default=1))
        dimension = f"A1:{_cell_ref(max_row - 1, max_col - 1)}"
        pane = ""
        if sheet.freeze_rows or sheet.freeze_columns:
            top_left = _cell_ref(sheet.freeze_rows, sheet.freeze_columns)
            active = "bottomRight" if sheet.freeze_rows and sheet.freeze_columns else (
                "bottomLeft" if sheet.freeze_rows else "topRight"
            )
            pane = (
                f'<pane xSplit="{sheet.freeze_columns}" ySplit="{sheet.freeze_rows}" '
                f'topLeftCell="{top_left}" activePane="{active}" state="frozen"/>'
            )
        show_grid = "1" if sheet.gridlines else "0"
        views = f'<sheetViews><sheetView workbookViewId="0" showGridLines="{show_grid}">{pane}</sheetView></sheetViews>'
        cols = ""
        if sheet.column_widths:
            cols = "<cols>" + "".join(
                f'<col min="{index + 1}" max="{index + 1}" width="{width}" customWidth="1"/>'
                for index, width in sorted(sheet.column_widths.items())
            ) + "</cols>"
        row_xml: list[str] = []
        for row_index, cells in enumerate(sheet.rows):
            cell_xml: list[str] = []
            for col_index, cell in enumerate(cells):
                if cell.value is None or cell.value == "":
                    continue
                ref = _cell_ref(row_index, col_index)
                style = f' s="{cell.style}"' if cell.style else ""
                number = _safe_number(cell.value)
                if isinstance(cell.value, numbers.Real) and not isinstance(cell.value, bool) and number is None:
                    continue
                if isinstance(cell.value, bool):
                    cell_xml.append(f'<c r="{ref}"{style} t="b"><v>{int(cell.value)}</v></c>')
                elif number is not None:
                    cell_xml.append(f'<c r="{ref}"{style}><v>{number}</v></c>')
                else:
                    text = _xml(cell.value)
                    preserve = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
                    cell_xml.append(f'<c r="{ref}"{style} t="inlineStr"><is><t{preserve}>{text}</t></is></c>')
            height = sheet.row_heights.get(row_index)
            height_attr = f' ht="{height}" customHeight="1"' if height else ""
            row_xml.append(f'<row r="{row_index + 1}"{height_attr}>{"".join(cell_xml)}</row>')
        merges = ""
        if sheet.merges:
            merges = f'<mergeCells count="{len(sheet.merges)}">' + "".join(
                f'<mergeCell ref="{_xml(ref)}"/>' for ref in sheet.merges
            ) + "</mergeCells>"
        auto_filter = f'<autoFilter ref="{sheet.auto_filter}"/>' if sheet.auto_filter else ""
        table_parts = ""
        if sheet.tables:
            parts = "".join(
                f'<tablePart r:id="rId{local_index}"/>'
                for local_index in range(1, len(sheet.tables) + 1)
            )
            table_parts = f'<tableParts count="{len(sheet.tables)}">{parts}</tableParts>'
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheetPr><tabColor rgb="FF{sheet.tab_color}"/></sheetPr><dimension ref="{dimension}"/>{views}'
            '<sheetFormatPr defaultRowHeight="18"/>'
            f'{cols}<sheetData>{"".join(row_xml)}</sheetData>{auto_filter}{merges}'
            '<pageMargins left="0.35" right="0.35" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
            f"{table_parts}</worksheet>"
        )

    @staticmethod
    def _sheet_rels(
        sheet: Sheet, sheet_index: int, table_ids: Mapping[tuple[int, int], int]
    ) -> str:
        relationships = "".join(
            f'<Relationship Id="rId{local_index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table{table_ids[(sheet_index, local_index)]}.xml"/>'
            for local_index in range(1, len(sheet.tables) + 1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{relationships}</Relationships>"
        )

    @staticmethod
    def _table_xml(table: TableSpec, table_id: int) -> str:
        ref = f"{_cell_ref(table.start_row, table.start_col)}:{_cell_ref(table.end_row, table.end_col)}"
        columns = "".join(
            f'<tableColumn id="{index}" name="{_xml(header)}"/>'
            for index, header in enumerate(table.headers, start=1)
        )
        safe_name = re.sub(r"[^A-Za-z0-9_]", "_", table.name)
        if not safe_name or safe_name[0].isdigit():
            safe_name = "Table_" + safe_name
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'id="{table_id}" name="{safe_name}" displayName="{safe_name}" ref="{ref}" totalsRowShown="0">'
            f'<autoFilter ref="{ref}"/><tableColumns count="{len(table.headers)}">{columns}</tableColumns>'
            '<tableStyleInfo name="TableStyleMedium2" showFirstColumn="0" showLastColumn="0" showRowStripes="1" showColumnStripes="0"/>'
            "</table>"
        )


def _styles_xml() -> str:
    fonts = [
        '<font><sz val="10"/><name val="Arial"/><color rgb="FF17333A"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FFFFFFFF"/></font>',
        '<font><b/><sz val="16"/><name val="Arial"/><color rgb="FF17333A"/></font>',
        '<font><i/><sz val="10"/><name val="Arial"/><color rgb="FF607379"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF1F4E5F"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FFB44A4A"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF4D7B61"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FFC97142"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF7A5C00"/></font>',
        '<font><sz val="10"/><name val="Arial"/><color rgb="FF718086"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF355C7D"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF7A6216"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF9C4A2D"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF7A3D13"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF6B5200"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF7A2946"/></font>',
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF9C1F1F"/></font>',
    ]
    fill_colors = [
        None,
        None,
        PALETTE["navy"],
        PALETTE["teal"],
        PALETTE["blue_light"],
        PALETTE["yellow_light"],
        PALETTE["red_light"],
        PALETTE["green_light"],
        "E7F3EA", "DDEBF7", "FFF2CC", "FCE4D6", "F4B183", "FFD966", "E4B7C5", "F4CCCC",
        PALETTE["orange_light"], PALETTE["grey_light"], PALETTE["teal_light"],
    ]
    fills = [
        '<fill><patternFill patternType="none"/></fill>',
        '<fill><patternFill patternType="gray125"/></fill>',
    ] + [
        f'<fill><patternFill patternType="solid"><fgColor rgb="FF{color}"/><bgColor indexed="64"/></patternFill></fill>'
        for color in fill_colors[2:]
    ]
    borders = [
        '<border><left/><right/><top/><bottom/><diagonal/></border>',
        '<border><left style="thin"><color rgb="FFDDE4E8"/></left><right style="thin"><color rgb="FFDDE4E8"/></right><top style="thin"><color rgb="FFDDE4E8"/></top><bottom style="thin"><color rgb="FFDDE4E8"/></bottom><diagonal/></border>',
        '<border><left/><right/><top/><bottom style="medium"><color rgb="FF1F4E5F"/></bottom><diagonal/></border>',
    ]

    def xf(font: int = 0, fill: int = 0, border: int = 0, num: int = 0,
           align: str = "", apply_num: bool = False) -> str:
        num_attr = ' applyNumberFormat="1"' if apply_num else ""
        alignment = f"<alignment {align}/>" if align else ""
        return (
            f'<xf numFmtId="{num}" fontId="{font}" fillId="{fill}" borderId="{border}" xfId="0"'
            f'{num_attr} applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">{alignment}</xf>'
        )

    cell_xfs = [
        xf(),
        xf(1, 2, 1, align='horizontal="center" vertical="center" wrapText="1"'),
        xf(2, 0, 2, align='vertical="center"'),
        xf(3, 0, 0, align='vertical="center" wrapText="1"'),
        xf(1, 3, 0, align='vertical="center"'),
        xf(0, 0, 0, align='vertical="center" wrapText="1"'),
        xf(0, 0, 0, 3, align='vertical="center" horizontal="center"', apply_num=True),
        xf(0, 0, 0, 165, align='vertical="center" horizontal="center"', apply_num=True),
        xf(0, 0, 0, 166, align='vertical="center" horizontal="center"', apply_num=True),
        xf(4, 4, 0, align='vertical="center" horizontal="center"'),
        xf(8, 5, 0, align='vertical="center" wrapText="1"'),
        xf(5, 6, 0, align='vertical="center" wrapText="1"'),
        xf(6, 7, 0, align='vertical="center" wrapText="1"'),
    ]
    tag_specs = [
        (6, 8), (10, 9), (11, 10), (12, 11),
        (13, 12), (14, 13), (15, 14), (16, 15),
    ]
    while len(cell_xfs) < STYLE_TAG_BASE:
        cell_xfs.append(xf())
    cell_xfs.extend(xf(font, fill, 0, align='vertical="center" wrapText="1"') for font, fill in tag_specs)
    while len(cell_xfs) < STYLE_LAYER_BASE:
        cell_xfs.append(xf())
    # L4, L3, L2, L1, L0, P
    cell_xfs.extend([
        xf(5, 6, 0, align='vertical="center" wrapText="1"'),
        xf(7, 16, 0, align='vertical="center" wrapText="1"'),
        xf(8, 5, 0, align='vertical="center" wrapText="1"'),
        xf(6, 7, 0, align='vertical="center" wrapText="1"'),
        xf(0, 18, 0, align='vertical="center" wrapText="1"'),
        xf(9, 17, 0, align='vertical="center" wrapText="1"'),
    ])
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2"><numFmt numFmtId="165" formatCode="0.0%"/><numFmt numFmtId="166" formatCode="0.00"/></numFmts>'
        f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts>'
        f'<fills count="{len(fills)}">{"".join(fills)}</fills>'
        f'<borders count="{len(borders)}">{"".join(borders)}</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="{len(cell_xfs)}">{"".join(cell_xfs)}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '<dxfs count="0"/><tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>'
        "</styleSheet>"
    )


LEVEL_FIELDS: tuple[tuple[str, str, float, int], ...] = (
    ("level_index", "关卡号", 9, STYLE_INTEGER),
    ("first_level_behavior_label", "首局综合层", 26, STYLE_WRAP),
    ("first_review_priority", "验收建议", 32, STYLE_WRAP),
    ("first_instance_count", "首局实例数", 11, STYLE_INTEGER),
    ("first_instance_player_weight", "首局玩家权重", 13, STYLE_INTEGER),
    ("first_classified_player_weight_share", "首局已分层覆盖", 14, STYLE_PERCENT),
    ("first_compound_high_burden_share", "首局复合卡点占比", 15, STYLE_PERCENT),
    ("first_any_friction_share", "首局任一卡顿占比", 15, STYLE_PERCENT),
    ("first_L4_share", "首局L4占比", 12, STYLE_PERCENT),
    ("first_L3_share", "首局L3占比", 12, STYLE_PERCENT),
    ("first_L2_share", "首局L2占比", 12, STYLE_PERCENT),
    ("first_L1_share", "首局L1占比", 12, STYLE_PERCENT),
    ("first_L0_share", "首局L0占比", 12, STYLE_PERCENT),
    ("first_pending_share", "首局待补样占比", 14, STYLE_PERCENT),
    ("first_main_high_burden_axes", "首局主要高负担轴", 30, STYLE_WRAP),
    ("first_top_instances", "首局优先实例", 68, STYLE_WRAP),
    ("retry_level_behavior_label", "非首局综合层", 26, STYLE_WRAP),
    ("retry_instance_player_weight", "非首局玩家权重", 15, STYLE_INTEGER),
    ("retry_classified_player_weight_share", "非首局已分层覆盖", 16, STYLE_PERCENT),
    ("retry_compound_high_burden_share", "非首局复合卡点占比", 17, STYLE_PERCENT),
    ("retry_any_friction_share", "非首局任一卡顿占比", 17, STYLE_PERCENT),
    ("retry_pending_share", "非首局待补样占比", 16, STYLE_PERCENT),
    ("retry_main_high_burden_axes", "非首局主要高负担轴", 31, STYLE_WRAP),
)

INSTANCE_FIELDS: tuple[tuple[str, str, float, int], ...] = (
    ("level_index", "关卡号", 9, STYLE_INTEGER),
    ("attempt_phase", "阶段", 9, STYLE_NORMAL),
    ("behavior_layer", "实例层级", 23, STYLE_WRAP),
    ("layer_stability", "分层稳定性", 22, STYLE_WRAP),
    ("player_count", "玩家N", 9, STYLE_INTEGER),
    ("sample_coverage", "样本覆盖", 13, STYLE_NORMAL),
    ("business_instance_key", "实际实例", 39, STYLE_WRAP),
    ("level_template_id", "模板", 16, STYLE_NORMAL),
    ("level_dup_para_id", "副本", 8, STYLE_INTEGER),
    ("level_dup_para", "DupPara", 15, STYLE_WRAP),
    ("primary_high_axis_count", "高负担轴数", 12, STYLE_INTEGER),
    ("primary_high_axes", "高负担轴", 28, STYLE_WRAP),
    ("primary_low_axes", "低负担轴", 25, STYLE_WRAP),
    ("primary_high_evidence", "主要高负担证据", 48, STYLE_WRAP),
    ("descriptive_behavior", "玩法特征", 34, STYLE_WRAP),
    ("supporting_evidence", "辅助证据", 42, STYLE_WRAP),
    ("primary_formal_signal_count", "正式主信号数", 13, STYLE_INTEGER),
    ("primary_high_confidence_signal_count", "高置信主信号数", 15, STYLE_INTEGER),
    ("primary_direction_agreement_median", "玩家方向一致率P50", 17, STYLE_PERCENT),
    ("primary_wilson90_low_median", "方向一致率下界P50", 18, STYLE_PERCENT),
    ("primary_split_half_agreement_median", "随机对半一致率P50", 18, STYLE_PERCENT),
    ("fail_rate_median", "失败率P50", 12, STYLE_PERCENT),
    ("duration_per_click_median", "每点击秒P50", 13, STYLE_DECIMAL),
    ("pressure_certain_share_median", "确定高压占比P50", 15, STYLE_PERCENT),
    ("visible_no_solution_share_median", "可见无解占比P50", 15, STYLE_PERCENT),
    ("manual_prop_per_100_clicks_median", "每百点击主动道具P50", 18, STYLE_DECIMAL),
    ("geometry_click_coverage_median", "几何覆盖", 12, STYLE_PERCENT),
    ("pattern_point_coverage_median", "花色覆盖", 12, STYLE_PERCENT),
)

METRIC_FIELDS: tuple[tuple[str, str, float, int], ...] = (
    ("role", "用途", 12, STYLE_NORMAL), ("axis", "行为轴", 16, STYLE_NORMAL),
    ("metric_label", "指标", 19, STYLE_NORMAL), ("definition", "如何计算", 58, STYLE_WRAP),
    ("interpretation", "如何理解", 48, STYLE_WRAP),
    ("candidate_instance_phase_count", "候选实例阶段", 14, STYLE_INTEGER),
    ("eligible_n_ge_10_count", "N≥10实例阶段", 14, STYLE_INTEGER),
    ("formal_signal_count", "正式信号", 11, STYLE_INTEGER),
    ("formal_rate_among_eligible", "正式信号率", 12, STYLE_PERCENT),
    ("high_burden_signal_count", "高负担信号", 12, STYLE_INTEGER),
    ("low_burden_signal_count", "低负担信号", 12, STYLE_INTEGER),
    ("descriptive_signal_count", "描述性信号", 12, STYLE_INTEGER),
    ("high_confidence_signal_count", "高置信信号", 12, STYLE_INTEGER),
    ("formal_player_count_median", "正式信号玩家N中位", 17, STYLE_DECIMAL),
    ("direction_agreement_median", "方向一致率P50", 14, STYLE_PERCENT),
    ("wilson90_low_median", "一致率下界P50", 15, STYLE_PERCENT),
    ("split_half_agreement_median", "随机对半一致率P50", 18, STYLE_PERCENT),
    ("split_half_exact_share", "随机对半完全一致", 17, STYLE_PERCENT),
    ("bootstrap_excludes_zero_share", "Bootstrap不跨零", 17, STYLE_PERCENT),
    ("leave_one_out_no_flip_share", "留一人不翻转", 15, STYLE_PERCENT),
    ("stability_summary", "稳定性结论", 31, STYLE_WRAP),
)


def _layer_style(value: Any) -> int:
    text = str(value or "")
    for offset, prefix in enumerate(("L4_", "L3_", "L2_", "L1_", "L0_", "P_")):
        if text.startswith(prefix):
            return STYLE_LAYER_BASE + offset
    return STYLE_WRAP


def _write_data_sheet(
    sheet: Sheet,
    records: Sequence[Mapping[str, Any]],
    fields: Sequence[tuple[str, str, float, int]],
    table_name: str,
    styled_field: str | None = None,
) -> None:
    headers = [field[1] for field in fields]
    sheet.write_row(0, headers, STYLE_HEADER)
    sheet.row_heights[0] = 32
    for column, field in enumerate(fields):
        sheet.column_widths[column] = field[2]
    for row_index, record in enumerate(records, start=1):
        values = [record.get(field[0]) for field in fields]
        styles = [field[3] for field in fields]
        if styled_field:
            for index, field in enumerate(fields):
                if field[0] == styled_field:
                    styles[index] = _layer_style(record.get(styled_field))
        sheet.write_row(row_index, values, styles)
        sheet.row_heights[row_index] = 24
    sheet.freeze_rows = 1
    sheet.freeze_columns = 2
    sheet.add_table(table_name, 0, 0, len(records), headers)


def write_stage1_workbook(
    path: str | Path,
    *,
    levels: Any,
    instances: Any,
    metrics: Any,
    summary: Mapping[str, Any],
    title: str = "玩家行为分层结论",
    subtitle: str = "按玩家内跨关差异与跨玩家复刻生成",
) -> Path:
    """Write the five-sheet phase-one workbook using static values."""
    level_rows, instance_rows, metric_rows = map(_records, (levels, instances, metrics))
    workbook = Workbook()
    overview = workbook.add_sheet("结论总览", PALETTE["navy"])
    level_sheet = workbook.add_sheet("关卡号分层", PALETTE["teal"])
    instance_sheet = workbook.add_sheet("实例分层", PALETTE["blue"])
    metric_sheet = workbook.add_sheet("行为维度", PALETTE["green"])
    method = workbook.add_sheet("方法与边界", PALETTE["grey"])

    overview.set(1, 0, title, STYLE_TITLE)
    overview.merges.append("A2:L2")
    overview.set(2, 0, subtitle, STYLE_SUBTITLE)
    overview.merges.append("A3:L3")
    overview.row_heights.update({1: 28, 2: 22})
    sample = summary.get("sample", {})
    overview.set(5, 0, "样本与范围", STYLE_SECTION)
    overview.merges.append("A6:C6")
    sample_rows = [
        ("玩家", sample.get("players", 0), "去重后的UID数"),
        ("有效对局", sample.get("valid_outcome_attempts", 0), "保留正常结算pass/fail"),
        ("回放动作", sample.get("decision_actions", 0), "按replay_info逐步解析"),
        ("业务实例", summary.get("business_instances", 0), "关卡号、模板、副本、规范化DupPara"),
        ("日期", f"{sample.get('date_min', '')} 至 {sample.get('date_max', '')}", f"{sample.get('product', '')} {sample.get('platform', '')}"),
    ]
    for row, values in enumerate(sample_rows, start=6):
        overview.write_row(row, values, [STYLE_WRAP, STYLE_WRAP, STYLE_WRAP])

    overview.set(5, 4, "分层覆盖", STYLE_SECTION)
    overview.merges.append("E6:G6")
    phase_counts = summary.get("phase_layer_counts", [])
    def layer_count(phase: str, layer: str) -> int:
        return int(next((r.get("instance_count", 0) for r in phase_counts if r.get("attempt_phase") == phase and r.get("behavior_layer") == layer), 0))
    coverage_rows = [
        ("首局L4", layer_count("首局", "L4_多维高负担"), "多维高负担实例"),
        ("首局L3", layer_count("首局", "L3_复合卡点"), "复合卡点实例"),
        ("首局L2", layer_count("首局", "L2_单点卡顿"), "单点卡顿实例"),
        ("首局有效覆盖", summary.get("first_classified_player_weight_share"), "玩家权重口径"),
        ("非首局有效覆盖", summary.get("retry_classified_player_weight_share"), "玩家权重口径"),
    ]
    for row, values in enumerate(coverage_rows, start=6):
        style = STYLE_PERCENT if row >= 9 else STYLE_INTEGER
        for offset, value in enumerate(values):
            overview.set(row, 4 + offset, value, [STYLE_WRAP, style, STYLE_WRAP][offset])

    overview.set(5, 8, "跨玩家稳定性", STYLE_SECTION)
    overview.merges.append("I6:L6")
    stability = summary.get("formal_signal_stability", {})
    stability_rows = [
        ("正式实例信号", summary.get("formal_instance_metric_signals", 0), "固定证据门槛"),
        ("方向一致率P50", stability.get("direction_agreement_median"), "同向玩家占比"),
        ("Wilson下界P50", stability.get("wilson90_low_median"), "方向一致率90%下界"),
        ("随机对半一致率均值", stability.get("split_half_agreement_mean"), "重复抽样方向一致"),
        ("留一人不翻转", stability.get("leave_one_out_no_flip_share"), "正式实例信号"),
    ]
    for row, values in enumerate(stability_rows, start=6):
        style = STYLE_INTEGER if row == 6 else STYLE_PERCENT
        for offset, value in enumerate(values):
            overview.set(row, 8 + offset, value, [STYLE_WRAP, style, STYLE_WRAP][offset])

    overview.set(13, 0, "实例层分布", STYLE_SECTION)
    overview.merges.append("A14:D14")
    overview.write_row(14, ["阶段", "层级", "实例数", "玩家权重占比"], STYLE_HEADER)
    for row, record in enumerate(phase_counts, start=15):
        overview.write_row(row, [record.get("attempt_phase"), record.get("behavior_layer"), record.get("instance_count"), record.get("player_weight_share")], [STYLE_NORMAL, _layer_style(record.get("behavior_layer")), STYLE_INTEGER, STYLE_PERCENT])

    overview.set(13, 5, "首局关卡号综合分布", STYLE_SECTION)
    overview.merges.append("F14:L14")
    overview.write_row(14, ["关卡号综合标签", "关卡数"], STYLE_HEADER)
    for row, (label, count) in enumerate(summary.get("first_level_label_counts", {}).items(), start=15):
        overview.set(row, 5, label, STYLE_WRAP)
        overview.set(row, 6, count, STYLE_INTEGER)

    overview.set(22, 5, "首局复合卡点占比最高的关卡号", STYLE_SECTION)
    overview.merges.append("F23:L23")
    top_headers = ["关卡", "综合标签", "复合卡点占比", "任一卡顿占比", "主要高负担轴"]
    for offset, header in enumerate(top_headers):
        overview.set(23, 5 + offset, header, STYLE_HEADER)
    for row, record in enumerate(summary.get("top_first_levels", [])[:6], start=24):
        values = [
            record.get("level_index"), record.get("level_behavior_label"),
            record.get("compound_high_burden_share"), record.get("any_friction_share"),
            record.get("main_high_burden_axes"),
        ]
        styles = [STYLE_INTEGER, STYLE_WRAP, STYLE_PERCENT, STYLE_PERCENT, STYLE_WRAP]
        for offset, (value, style) in enumerate(zip(values, styles)):
            overview.set(row, 5 + offset, value, style)

    overview.set(28, 0, "首局L4代表实例", STYLE_SECTION)
    overview.merges.append("A29:D29")
    overview.write_row(29, ["关卡", "实际实例", "玩家N", "高负担轴"], STYLE_HEADER)
    for row, record in enumerate(summary.get("first_l4_instances", [])[:7], start=30):
        overview.write_row(row, [
            record.get("level_index"), record.get("business_instance_key"),
            record.get("player_count"), record.get("primary_high_axes"),
        ], [STYLE_INTEGER, STYLE_WRAP, STYLE_INTEGER, STYLE_WRAP])

    overview.set(38, 0, "可用范围与下一步", STYLE_SECTION)
    overview.merges.append("A39:L39")
    notes = [
        ("当前解释", "L4至L0描述实时过程负担，不等同体验好坏或静态模板难度。"),
        ("分层单位", "关卡号＋模板ID＋副本号＋规范化DupPara；首局与非首局分开。"),
        ("活动背包+1", "自动生效与玩家主动使用分开；容量未知时按7/8格上下界处理。"),
        ("应用边界", "已有足够真实玩家数据的线上关可批量跑；新关上线前需模拟玩家。"),
        ("优先优化", "加入完整局面求解器、复杂道具效果模拟和明确容量来源。"),
    ]
    for row, (key, value) in enumerate(notes, start=39):
        overview.set(row, 0, key, STYLE_WRAP)
        overview.set(row, 1, value, STYLE_WRAP)
        overview.merges.append(f"B{row + 1}:L{row + 1}")
        overview.row_heights[row] = 28
    for index, width in enumerate((12, 34, 18, 27, 12, 28, 14, 14, 19, 17, 19, 12)):
        overview.column_widths[index] = width

    _write_data_sheet(level_sheet, level_rows, LEVEL_FIELDS, "Stage1LevelLayers", "first_level_behavior_label")
    _write_data_sheet(instance_sheet, instance_rows, INSTANCE_FIELDS, "Stage1InstanceLayers", "behavior_layer")
    _write_data_sheet(metric_sheet, metric_rows, METRIC_FIELDS, "Stage1BehaviorDimensions")

    method.set(1, 0, "方法、层级与使用边界", STYLE_TITLE)
    method.merges.append("A2:L2")
    method.set(2, 0, "面向关卡策划的固定分析口径", STYLE_SUBTITLE)
    method.merges.append("A3:L3")
    method.set(5, 0, "怎么拆解和计算", STYLE_SECTION)
    method.merges.append("A6:L6")
    steps = [
        (1, "逐步解析回放", "把interval_ms累加成局内时间，并区分点击、主动道具、自动背包+1和其他动作。"),
        (2, "还原实时局面", "用模板和配置还原每次点击前的可见、可点、花色、遮挡与背包。"),
        (3, "提取对局特征", "统计节奏、压力、路径压缩、认知停滞、选择陷阱、道具和结果。"),
        (4, "建立玩家基线", "同阶段排除当前关卡号，用该玩家其他关卡中位数与MAD定义正常水平。"),
        (5, "检查跨玩家复刻", "同一实例至少10人，并用方向一致率、Wilson、Bootstrap、随机对半和留一人检查。"),
        (6, "固定规则分层", "稳定高负担轴数量决定L4至L0；实例阶段少于10人保留P，不按分位数分名额。"),
    ]
    for row, values in enumerate(steps, start=6):
        method.write_row(row, values, [STYLE_INTEGER, STYLE_WRAP, STYLE_WRAP])
        method.merges.append(f"C{row + 1}:L{row + 1}")
        method.row_heights[row] = 34
    method.set(14, 0, "实例层级", STYLE_SECTION)
    method.merges.append("A15:L15")
    method.write_row(15, ["层级", "固定规则", "策划含义"], STYLE_HEADER)
    layer_defs = [
        ("L4_多维高负担", "至少3个主分层高负担轴", "多种实时困难同时出现"),
        ("L3_复合卡点", "2个主分层高负担轴", "存在稳定复合卡点"),
        ("L2_单点卡顿", "1个主分层高负担轴", "存在稳定单一过程问题"),
        ("L1_稳定低负担", "无高负担轴且至少1个低负担轴", "相对玩家自身水平更顺畅"),
        ("L0_未见稳定过程差异", "N≥10且未检出稳定过程差异", "不等于绝对简单"),
        ("P_待补样", "实例阶段N<10", "保留实例，不强行定级"),
    ]
    for row, values in enumerate(layer_defs, start=16):
        method.write_row(row, values, [_layer_style(values[0]), STYLE_WRAP, STYLE_WRAP])
    method.set(24, 0, "当前边界", STYLE_SECTION)
    method.merges.append("A25:L25")
    limitations = [
        ("全局可解性", "可见无解和短窗路径解释玩家眼前卡点，但不证明完整隐藏牌面的全局无解。"),
        ("复杂道具", "洗牌、移动和复活后会降低花色状态置信度；不猜测未模拟效果。"),
        ("背包容量", "活动限时背包+1可能自动生效；自动效果不计入玩家主动道具依赖。"),
        ("人群外推", "不同平台、进度、付费与版本人群应按同一流程单独复刻。"),
        ("体验判断", "高过程负担可对应合理挑战，也可能是恶意卡顿，仍需策划少量人工标注。"),
    ]
    for row, (key, value) in enumerate(limitations, start=25):
        method.set(row, 0, key, STYLE_WRAP)
        method.set(row, 1, value, STYLE_WRAP)
        method.merges.append(f"B{row + 1}:L{row + 1}")
        method.row_heights[row] = 32
    for index, width in enumerate((12, 22, 62, 12, 12, 12, 12, 12, 12, 12, 12, 12)):
        method.column_widths[index] = width
    return workbook.save(path)


TAG_COLORS = {f"T{index}": STYLE_TAG_BASE + index for index in range(8)}
PLANNER_COLORS = {
    "P0": STYLE_SUCCESS,
    "P1": STYLE_BLUE_CELL,
    "P2": STYLE_TAG_BASE + 2,
    "P3": STYLE_TAG_BASE + 4,
    "P4": STYLE_WARNING,
    "P5": STYLE_DANGER,
    "PX": STYLE_DANGER,
    "PS": STYLE_LAYER_BASE + 5,
}


def write_stage2_workbook(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Write the planner-facing sequence, duplicate labels and audit sheets."""
    rows = list(payload.get("rows", []))
    if not rows:
        raise ValueError("第二阶段没有可写出的关卡结果")
    metadata = dict(payload.get("metadata", {}))
    audit_rows = list(payload.get("audit_rows", []))
    duplicate_rows = list(payload.get("duplicate_rows", []))
    mapping_rows = list(payload.get("level_list_audit_rows", []))
    workbook = Workbook()
    sheet = workbook.add_sheet("原序列与推荐序列", PALETTE["navy"])
    duplicate = workbook.add_sheet("关卡副本标签", PALETTE["teal"])
    mapping = workbook.add_sheet("配置重排对照", PALETTE["blue"])
    audit = workbook.add_sheet("内部计算审计", PALETTE["grey"])

    sheet.set(1, 0, f"{metadata.get('level_range', '')}关关卡分类与推荐序列", STYLE_TITLE)
    sheet.merges.append("A2:R2")
    optimize_count = int(metadata.get("optimize_count", 0) or 0)
    sheet.set(2, 0, (
        "排关压力标签以玩家实时行为为主；消耗与失败流失只用于区分挑战价值与负面风险。"
        f"当前有 {optimize_count} 关建议优化/替换，这些关不给正式排位。"
    ), STYLE_SUBTITLE)
    sheet.merges.append("A3:R3")
    strategy_code = metadata.get("sequence_strategy_code", "dual_peak")
    strategy_name = metadata.get("sequence_strategy_name", "双峰阶梯")
    strategy_description = metadata.get(
        "sequence_strategy_description", metadata.get("sequence_rule", "")
    )
    cycle_display = metadata.get("sequence_cycle_display", "")
    sheet.set(
        3,
        0,
        f"排关方案：{strategy_name}（{strategy_code}）。{strategy_description} 十关角色：{cycle_display}",
        STYLE_WRAP,
    )
    sheet.merges.append("A4:R4")
    sheet.row_heights[3] = 44
    sheet.write_row(5, ["排关压力标签", "判定逻辑", "关卡数"], STYLE_HEADER)
    definitions = metadata.get("planner_tag_definitions", [])
    for row_index, item in enumerate(definitions, start=6):
        code = str(item.get("code", ""))
        sheet.write_row(row_index, [f"{code} {item.get('name', '')}", item.get("definition", ""), item.get("count", 0)], [PLANNER_COLORS.get(code, STYLE_WRAP), STYLE_WRAP, STYLE_INTEGER])
        sheet.row_heights[row_index] = 25

    header_row = 15
    headers = ["原关卡号", "关卡ID", "模板ID", "最终标签口径", "排关压力标签", "全实例加权标签",
               "主副本代表标签", "标签来源副本号", "标签来源副本参数", "来源副本进入人数", "来源副本进入占比",
               "适合位置", "内部三维诊断", "三维证据", "风险备注", "建议处理", "排位状态", "新推荐序列后关卡号"]
    sheet.write_row(header_row, headers, STYLE_HEADER)
    fields = ["original_level_index", "level_id", "level_template_id", "level_label_method_name", "planner_tag", "weighted_planner_tag",
              "dominant_duplicate_planner_tag", "dominant_duplicate_id", "dominant_duplicate_para", "dominant_duplicate_enter_users", "dominant_duplicate_enter_share",
              "slot_guidance", "named_tag", "feature_summary", "risk_note", "action", "placement_status", "recommended_level_index"]
    for row_index, record in enumerate(rows, start=header_row + 1):
        code = str(record.get("planner_tag_code", ""))
        styles = [STYLE_INTEGER, STYLE_NORMAL, STYLE_NORMAL, STYLE_WRAP, PLANNER_COLORS.get(code, STYLE_WRAP), STYLE_WRAP,
                  STYLE_WRAP, STYLE_NORMAL, STYLE_WRAP, STYLE_INTEGER, STYLE_PERCENT,
                  STYLE_WRAP, STYLE_WRAP, STYLE_WRAP, STYLE_DANGER if code == "PX" else STYLE_WRAP,
                  STYLE_DANGER if code == "PX" else STYLE_WRAP, STYLE_WRAP, STYLE_BLUE_CELL]
        sheet.write_row(row_index, [record.get(field) for field in fields], styles)
        sheet.row_heights[row_index] = 28
    for index, width in enumerate((12, 31, 20, 28, 22, 22, 22, 14, 20, 16, 16, 23, 22, 32, 50, 38, 22, 20)):
        sheet.column_widths[index] = width
    sheet.freeze_rows = header_row + 1
    sheet.freeze_columns = 1
    sheet.add_table("LevelSequenceRecommendation", header_row, 0, len(rows), headers)

    duplicate.set(1, 0, "每个关卡副本的行为压力标签", STYLE_TITLE)
    duplicate.merges.append("A2:U2")
    duplicate.set(2, 0, "严格按关卡号＋模板ID＋副本号＋副本参数＋首局/非首局分开；样本不足的副本不强行定层。", STYLE_SUBTITLE)
    duplicate.merges.append("A3:U3")
    duplicate_fields = [
        ("level_index", "关卡号", STYLE_INTEGER, 11), ("level_id", "关卡ID", STYLE_NORMAL, 29),
        ("level_template_id", "模板ID", STYLE_NORMAL, 18), ("level_dup_para_id", "副本号", STYLE_NORMAL, 10),
        ("level_dup_para", "副本参数", STYLE_WRAP, 18), ("attempt_phase", "对局阶段", STYLE_NORMAL, 11),
        ("player_count", "玩家数", STYLE_INTEGER, 10), ("behavior_layer", "实例行为层", STYLE_WRAP, 21),
        ("layer_stability", "稳定性", STYLE_NORMAL, 12), ("primary_high_axes", "主要高负担维度", STYLE_WRAP, 28),
        ("duplicate_planner_tag", "副本压力标签", STYLE_WRAP, 22), ("level_planner_tag", "关卡综合压力标签", STYLE_WRAP, 22),
        ("slot_guidance", "适合位置", STYLE_WRAP, 22), ("enter_users", "线上进入人数", STYLE_INTEGER, 15),
        ("consume", "人均消耗", STYLE_DECIMAL, 12), ("consume_band", "消耗档位", STYLE_NORMAL, 11),
        ("fail_loss", "失败流失", STYLE_PERCENT, 12), ("fail_band", "流失档位", STYLE_NORMAL, 11),
        ("duplicate_risk_note", "副本风险备注", STYLE_WRAP, 48),
        ("action", "建议处理", STYLE_WRAP, 40),
        ("is_dominant_online_duplicate", "是否主副本标签来源", STYLE_INTEGER, 20),
    ]
    duplicate_header = 5
    duplicate_headers = [item[1] for item in duplicate_fields]
    duplicate.write_row(duplicate_header, duplicate_headers, STYLE_HEADER)
    for row_index, record in enumerate(duplicate_rows, start=duplicate_header + 1):
        styles = [item[2] for item in duplicate_fields]
        styles[10] = PLANNER_COLORS.get(str(record.get("duplicate_planner_code", "PS")), STYLE_WRAP)
        duplicate.write_row(row_index, [record.get(item[0]) for item in duplicate_fields], styles)
    for index, item in enumerate(duplicate_fields):
        duplicate.column_widths[index] = item[3]
    duplicate.freeze_rows = duplicate_header + 1
    duplicate.freeze_columns = 3
    if duplicate_rows:
        duplicate.add_table("DuplicatePressureLabels", duplicate_header, 0, len(duplicate_rows), duplicate_headers)

    mapping.set(1, 0, "配置重排对照", STYLE_TITLE)
    mapping.merges.append("A2:R2")
    mapping.set(2, 0, "源配置始终只读；合理关正常排位，待优化/补样关保留原位但四列关卡 ID 留空。", STYLE_SUBTITLE)
    mapping.merges.append("A3:R3")
    mapping_fields = [
        ("original_level_index", "原关卡号"), ("recommended_level_index", "新关卡号"),
        ("reserved_blank_level_index", "留空位置"), ("mapping_status", "映射状态"), ("planner_tag", "排关压力标签"),
        ("sequence_strategy_name", "排列方案"),
        ("source_LevelId", "原 LevelId"), ("target_before_LevelId", "目标位原 LevelId"), ("target_after_LevelId", "目标位新 LevelId"),
        ("source_DDALevelId1", "原 DDALevelId1"), ("target_before_DDALevelId1", "目标位原 DDALevelId1"), ("target_after_DDALevelId1", "目标位新 DDALevelId1"),
        ("source_DDALevelId2", "原 DDALevelId2"), ("target_before_DDALevelId2", "目标位原 DDALevelId2"), ("target_after_DDALevelId2", "目标位新 DDALevelId2"),
        ("source_DDALevelId3", "原 DDALevelId3"), ("target_before_DDALevelId3", "目标位原 DDALevelId3"), ("target_after_DDALevelId3", "目标位新 DDALevelId3"),
    ]
    mapping_header = 5
    mapping_headers = [item[1] for item in mapping_fields]
    for index in range(len(mapping_fields)):
        mapping.column_widths[index] = 18 if index < 3 else 28
    if mapping_rows:
        mapping.write_row(mapping_header, mapping_headers, STYLE_HEADER)
        for row_index, record in enumerate(mapping_rows, start=mapping_header + 1):
            mapping.write_row(row_index, [record.get(item[0]) for item in mapping_fields], STYLE_WRAP)
        mapping.freeze_rows = mapping_header + 1
        mapping.freeze_columns = 2
        mapping.add_table("ConfigReorderMapping", mapping_header, 0, len(mapping_rows), mapping_headers)
    else:
        mapping.set(5, 0, "未生成配置映射，请查看运行日志。", STYLE_WARNING)
        mapping.merges.append("A6:R6")

    audit.set(1, 0, "内部计算审计", STYLE_TITLE)
    audit.merges.append("A2:R2")
    audit.set(2, 0, "用于复查稳健基线、排关方案、逐关风险桶、目标尾号、标签、排位分数与配置来源。", STYLE_SUBTITLE)
    audit.merges.append("A3:R3")
    level_list_source = metadata.get("level_list_source") or {}
    diversity_before = metadata.get("sequence_diversity_before") or {}
    diversity_after = metadata.get("sequence_diversity_after") or {}
    audit_pairs = [
        ("关卡范围", metadata.get("level_range")),
        ("关卡数", metadata.get("level_count")),
        ("排关方案代码", strategy_code),
        ("排关方案名称", strategy_name),
        ("关卡标签聚合方式", metadata.get("level_label_method_name")),
        ("标签聚合说明", metadata.get("level_label_method_description")),
        ("方案说明", strategy_description),
        ("十关角色", cycle_display),
        ("风险桶→尾号", metadata.get("sequence_bucket_to_digit_display")),
        ("人均消耗中心", metadata.get("consume_center")),
        ("人均消耗尺度", metadata.get("consume_scale")),
        ("失败流失中心", metadata.get("fail_loss_center")),
        ("失败流失尺度", metadata.get("fail_loss_scale")),
        ("稳健带规则", metadata.get("band_rule")),
        (
            "相邻重复优化",
            f"同模板 {diversity_before.get('adjacent_same_template_count', 0)}→{diversity_after.get('adjacent_same_template_count', 0)}；"
            f"相邻T6/T7 {diversity_before.get('adjacent_t6_t7_count', 0)}→{diversity_after.get('adjacent_t6_t7_count', 0)}",
        ),
        ("路由配置源", level_list_source.get("source_path", "")),
        ("路由配置SHA256", level_list_source.get("source_sha256", "")),
    ]
    for row_index, (key, value) in enumerate(audit_pairs, start=5):
        audit.set(row_index, 0, key, STYLE_WRAP)
        audit.set(row_index, 1, value, STYLE_WRAP)
        audit.merges.append(f"B{row_index + 1}:R{row_index + 1}")
    audit_header_row = 21
    audit_fields = [
        ("level_index", "原关卡号", STYLE_INTEGER, 11),
        ("level_id", "关卡ID", STYLE_NORMAL, 28),
        ("level_template_id", "模板ID", STYLE_NORMAL, 18),
        ("first_level_behavior_label", "压力标签", STYLE_WRAP, 26),
        ("pressure_order", "压力序", STYLE_INTEGER, 10),
        ("consume", "人均消耗", STYLE_DECIMAL, 12),
        ("consume_z", "消耗稳健z", STYLE_DECIMAL, 13),
        ("consume_band", "消耗档位", STYLE_NORMAL, 11),
        ("fail_loss", "失败流失", STYLE_PERCENT, 12),
        ("fail_z", "流失稳健z", STYLE_DECIMAL, 13),
        ("fail_band", "流失档位", STYLE_NORMAL, 11),
        ("planner_tag", "排关压力标签", STYLE_WRAP, 22),
        ("slot_guidance", "适合位置", STYLE_WRAP, 22),
        ("named_tag", "内部三维诊断", STYLE_WRAP, 22),
        ("sequence_score", "序列难度分", STYLE_DECIMAL, 14),
        ("sequence_risk_bucket", "风险桶(0低-9高)", STYLE_INTEGER, 15),
        ("sequence_slot_digit", "目标尾号", STYLE_INTEGER, 11),
        ("sequence_role", "十关角色", STYLE_WRAP, 14),
        ("recommended_level_index", "推荐关卡号", STYLE_INTEGER, 13),
        ("sequence_strategy_code", "方案代码", STYLE_NORMAL, 16),
    ]
    audit_headers = [field[1] for field in audit_fields]
    audit.write_row(audit_header_row, audit_headers, STYLE_HEADER)
    for row_index, record in enumerate(audit_rows, start=audit_header_row + 1):
        code = str(record.get("tag_code", ""))
        styles = [field[2] for field in audit_fields]
        planner_code = str(record.get("planner_tag_code", ""))
        styles[11] = PLANNER_COLORS.get(planner_code, STYLE_WRAP)
        audit.write_row(row_index, [record.get(field[0]) for field in audit_fields], styles)
    for index, field in enumerate(audit_fields):
        audit.column_widths[index] = field[3]
    audit.freeze_rows = audit_header_row + 1
    audit.freeze_columns = 1
    audit.add_table("LevelSequenceAudit", audit_header_row, 0, len(audit_rows), audit_headers)
    return workbook.save(path)


def inspect_xlsx(path: str | Path) -> dict[str, Any]:
    """Return a lightweight integrity report used by tests and the server."""
    target = Path(path)
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        required = {"[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml"}
        missing = sorted(required - names)
        if missing:
            raise ValueError(f"XLSX缺少核心部件: {missing}")
        xml_files = [name for name in names if name.endswith(".xml")]
        for name in xml_files:
            fromstring(archive.read(name))
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        sheet_names = re.findall(r'<sheet name="([^"]+)"', workbook_xml)
        table_count = sum(name.startswith("xl/tables/table") for name in names)
    return {
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "sheet_names": sheet_names,
        "table_count": table_count,
        "xml_part_count": len(xml_files),
    }
