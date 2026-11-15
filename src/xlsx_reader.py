# -*- coding: utf-8 -*-
"""极简 xlsx / csv 表格读取（仅标准库）。

用途：批量导入主播列表（Excel/CSV），打包版无需 openpyxl。
支持：
  - .xlsx / .xlsm：按 workbook.xml 读取**全部工作表**（保留顺序与表名）；
  - .csv / .tsv / .txt：按文本读取。
"""
from __future__ import annotations

import csv
import io
import os
import xml.etree.ElementTree as ET
import zipfile
from typing import List, Sequence, Tuple

Ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RelsNs = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_index(ref: str) -> int:
    """A1 -> 0, B1 -> 1, AA1 -> 26。"""
    idx = 0
    for ch in ref:
        if ch.isalpha():
            idx = idx * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(0, idx - 1)


def _read_shared_strings(zf: zipfile.ZipFile, names: List[str]) -> List[str]:
    shared: List[str] = []
    if "xl/sharedStrings.xml" not in names:
        return shared
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except ET.ParseError:
        return shared
    for si in root.findall(Ns + "si"):
        shared.append("".join(t.text or "" for t in si.iter(Ns + "t")))
    return shared


def _read_worksheet(zf: zipfile.ZipFile, path: str, shared: List[str]) -> List[List[str]]:
    rows: List[List[str]] = []
    try:
        root = ET.fromstring(zf.read(path))
    except (KeyError, ET.ParseError):
        return rows
    for row in root.iter(Ns + "row"):
        cells = {}
        max_col = -1
        for c in row.findall(Ns + "c"):
            ref = c.get("r") or ""
            col = _col_index(ref)
            ctype = c.get("t")
            value = ""
            if ctype == "s":
                v = c.find(Ns + "v")
                if v is not None and v.text is not None:
                    try:
                        value = shared[int(v.text)]
                    except (ValueError, IndexError):
                        value = ""
            elif ctype == "inlineStr":
                is_el = c.find(Ns + "is")
                if is_el is not None:
                    value = "".join(t.text or "" for t in is_el.iter(Ns + "t"))
            else:
                v = c.find(Ns + "v")
                value = (v.text or "") if v is not None else ""
            cells[col] = str(value).strip()
            max_col = max(max_col, col)
        if max_col < 0:
            continue
        rows.append([cells.get(i, "") for i in range(max_col + 1)])
    return rows


def _sheet_entries(zf: zipfile.ZipFile, names: List[str]) -> List[Tuple[str, str]]:
    """返回 [(工作表名, zip 内路径)]，按 workbook.xml 中的顺序。"""
    entries: List[Tuple[str, str]] = []
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        try:
            rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rel_root:
                target = rel.get("Target") or ""
                if target.startswith("/"):
                    target = target.lstrip("/")
                elif not target.startswith("xl/"):
                    target = "xl/" + target
                rels[rel.get("Id")] = target
        except ET.ParseError:
            rels = {}
    if "xl/workbook.xml" in names:
        try:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            for sheet in wb.iter(Ns + "sheet"):
                name = sheet.get("name") or ""
                rid = sheet.get(RelsNs + "id") or sheet.get("id") or ""
                target = rels.get(rid)
                if target and target in names:
                    entries.append((name or os.path.basename(target), target))
        except ET.ParseError:
            entries = []
    if entries:
        return entries
    # 兜底：按文件名排序（旧文件可能没有 workbook.xml）
    worksheets = sorted(n for n in names
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    return [("Sheet%d" % (i + 1), n) for i, n in enumerate(worksheets)]


def read_xlsx_sheets(path: str) -> List[Tuple[str, List[List[str]]]]:
    """读取 xlsx/xlsm 的**全部工作表**，返回 [(sheet 名, 行列表)]（保留工作簿顺序）。"""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared = _read_shared_strings(zf, names)
        return [(name, _read_worksheet(zf, target, shared))
                for name, target in _sheet_entries(zf, names)]


def read_xlsx(path: str) -> List[List[str]]:
    """读取 xlsx 第一个工作表（兼容旧接口），返回二维文本列表。"""
    sheets = read_xlsx_sheets(path)
    return sheets[0][1] if sheets else []


def _decode_csv(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def read_csv(path: str, delimiter: str = ",") -> List[List[str]]:
    text = _decode_csv(open(path, "rb").read())
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [[str(c).strip() for c in row] for row in reader]


def read_table_sheets(path: str) -> List[Tuple[str, List[List[str]]]]:
    """自动识别扩展名读取**全部工作表**：xlsx -> 多 sheet；csv/txt -> 单个表。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx_sheets(path)
    if ext in (".csv", ".tsv", ".txt"):
        delim = "\t" if ext == ".tsv" else ","
        return [("CSV", read_csv(path, delim))]
    if ext == ".xls":
        raise ValueError("不支持旧版 .xls，请用 Excel 另存为 .xlsx")
    try:
        return read_xlsx_sheets(path)
    except Exception:
        return [("CSV", read_csv(path))]


def read_table(path: str) -> List[List[str]]:
    """自动识别扩展名读取表格（兼容旧接口，只返回第一个 sheet）。"""
    sheets = read_table_sheets(path)
    return sheets[0][1] if sheets else []
