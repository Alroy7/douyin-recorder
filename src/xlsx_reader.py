# -*- coding: utf-8 -*-
"""极简 xlsx / csv 表格读取（仅标准库）。

用途：批量导入主播列表（Excel/CSV），打包版无需 openpyxl。
支持 .xlsx（zip + XML 解析 sharedStrings / 首个 worksheet）与 .csv（utf-8-sig / gbk）。
"""
from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from typing import List, Sequence, Tuple

Ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    """A1 -> 0, B1 -> 1, AA1 -> 26。"""
    idx = 0
    for ch in ref:
        if ch.isalpha():
            idx = idx * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(0, idx - 1)


def read_xlsx(path: str) -> List[List[str]]:
    """读取 xlsx 第一个工作表，返回二维文本列表（空单元格为空串）。"""
    rows: List[List[str]] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(Ns + "si"):
                shared.append("".join(t.text or "" for t in si.iter(Ns + "t")))
        sheets = sorted(n for n in names
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            return rows
        import xml.etree.ElementTree as ET
        root = ET.fromstring(zf.read(sheets[0]))
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


def read_table(path: str) -> List[List[str]]:
    """自动识别扩展名读取表格。.xlsx/.xlsm -> xlsx；.csv/.txt -> csv。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(path)
    if ext in (".csv", ".tsv", ".txt"):
        delim = "\t" if ext == ".tsv" else ","
        return read_csv(path, delim)
    if ext == ".xls":
        raise ValueError("不支持旧版 .xls，请用 Excel 另存为 .xlsx")
    # 兜底：先按 xlsx 尝试
    try:
        return read_xlsx(path)
    except Exception:
        return read_csv(path)
