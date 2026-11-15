# -*- coding: utf-8 -*-
"""导入功能测试：xlsx/csv 读取、表格解析、去重（不联网、无第三方依赖）。"""
import csv
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src import import_util, xlsx_reader

TMP = ROOT / "tests" / "tmp" / "import"
TMP.mkdir(parents=True, exist_ok=True)


def make_xlsx(path: Path, rows):
    """最小 xlsx：sharedStrings + sheet1.xml（读取器只需要这两个）。"""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = []
    index = {}
    sheet_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, val in enumerate(row):
            val = "" if val is None else str(val)
            if val not in index:
                index[val] = len(shared)
                shared.append(val)
            col = chr(65 + c) if c < 26 else "A"
            cells.append('<c r="%s%d" t="s"><v>%d</v></c>' % (col, r, index[val]))
        sheet_rows.append('<row r="%d">%s</row>' % (r, "".join(cells)))
    sheet = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
             % (ns, "".join(sheet_rows)))
    shared_xml = ('<?xml version="1.0" encoding="UTF-8"?><sst xmlns="%s">%s</sst>'
                  % (ns, "".join("<si><t>%s</t></si>" % v.replace("&", "&amp;") for v in shared)))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


def test_xlsx_roundtrip():
    p = TMP / "t.xlsx"
    make_xlsx(p, [["序号", "账号", "主页链接"],
                  ["1", "主播A", "https://www.douyin.com/user/MS4wAAAA"],
                  ["2", "主播B", "https://live.douyin.com/123456789"]])
    rows = xlsx_reader.read_table(str(p))
    assert len(rows) == 3, rows
    assert rows[0] == ["序号", "账号", "主页链接"], rows[0]
    assert rows[1][2].startswith("https://www.douyin.com/user/"), rows[1]
    print("xlsx roundtrip OK")


def test_csv_gbk():
    p = TMP / "t.csv"
    text = "账号,主页链接\n主播C,https://www.douyin.com/user/MS4wBBBB\n"
    p.write_bytes(text.encode("gbk"))
    rows = xlsx_reader.read_table(str(p))
    assert rows[1][0] == "主播C", rows
    print("csv gbk OK")


def test_records_and_dedupe():
    rows = [["序号", "账号", "主页链接"],
            ["1", "甲", "https://www.douyin.com/user/AAA"],
            ["2", "乙", "https://www.douyin.com/user/BBB"],
            ["3", "甲重复", "https://www.douyin.com/user/AAA"],
            ["4", "同一房间", "https://live.douyin.com/555555"],
            ["5", "同一房间2", "555555"],
            ["6", "", ""],
            ["7", "无效", "不是链接"]]
    records, skipped = import_util.records_from_table(rows)
    accepted, dup, invalid = import_util.dedupe(records)
    keys = [import_util.target_key(r["target"]) for r in accepted]
    assert len(accepted) == 3, (len(accepted), accepted)
    assert dup == 2, dup
    assert skipped == 2, skipped
    assert "sec:aaa" in keys, keys
    assert "rid:555555" in keys, keys
    # 与已有列表去重
    existing = {"sec:aaa"}
    accepted2, dup2, _ = import_util.dedupe(records, existing)
    assert dup2 >= 1 and all(import_util.target_key(r["target"]) != "sec:aaa" for r in accepted2)
    print("records/dedupe OK: accepted=%d dup=%d invalid=%d skipped=%d" % (
        len(accepted), dup, invalid, skipped))


def test_url_normalize():
    a = import_util.target_key("https://www.douyin.com/user/MS4wAAA?from=share")
    b = import_util.target_key("https://douyin.com/user/MS4wAAA")
    assert a == b == "sec:ms4waaa", (a, b)
    c = import_util.target_key("https://live.douyin.com/123456?enter_from=web")
    d = import_util.target_key("123456")
    assert c == d == "rid:123456", (c, d)
    print("url normalize OK")


def main():
    test_xlsx_roundtrip()
    test_csv_gbk()
    test_records_and_dedupe()
    test_url_normalize()
    print("ALL IMPORT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
