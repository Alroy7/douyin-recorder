# -*- coding: utf-8 -*-
"""极简 xlsx 写入（仅标准库，打包版不依赖 openpyxl）。

只写一个工作表、单元格全部用 inlineStr（字符串），列宽按内容估算。
读取端 src/xlsx_reader.py 已支持 inlineStr，可无损往返。
"""
from __future__ import annotations

import os
import zipfile
from typing import Sequence
from xml.sax.saxutils import escape

__all__ = ["write_xlsx", "col_name"]


def col_name(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA。"""
    name = ""
    n = int(idx) + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell_xml(ref: str, value: str) -> str:
    text = escape(str(value), {'"': "&quot;"})
    return ('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
            % (ref, text))


def write_xlsx(path: str, rows: Sequence[Sequence[object]],
               sheet_name: str = "主播列表") -> None:
    """把二维表格写成 xlsx（第一行视为表头，加粗样式省略，仅设列宽）。"""
    safe_sheet = escape(str(sheet_name))[:31] or "Sheet1"
    sheet_rows = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx, value in enumerate(row):
            ref = "%s%d" % (col_name(c_idx), r_idx)
            cells.append(_cell_xml(ref, "" if value is None else value))
        sheet_rows.append('<row r="%d">%s</row>' % (r_idx, "".join(cells)))
    # 列宽：取该列最长文本（中文按 2 个宽度估算），限制 10~60
    widths = []
    ncols = max((len(r) for r in rows), default=0)
    for c in range(ncols):
        longest = 0
        for row in rows:
            if c < len(row):
                text = str(row[c] or "")
                w = sum(2 if ord(ch) > 127 else 1 for ch in text)
                longest = max(longest, w)
        widths.append(min(60, max(10, longest + 2)))
    cols_xml = "".join('<col min="%d" max="%d" width="%d" customWidth="1"/>'
                       % (i + 1, i + 1, w) for i, w in enumerate(widths))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols>%s</cols><sheetData>%s</sheetData></worksheet>'
        % (cols_xml, "".join(sheet_rows))
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>'
        % safe_sheet
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    tmp = str(path) + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    os.replace(tmp, str(path))
