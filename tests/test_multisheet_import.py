# -*- coding: utf-8 -*-
"""多工作表 Excel 导入回归：读取全部 sheet、每个 sheet 独立识别表头。"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "multisheet"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src import import_util
from src.xlsx_reader import read_xlsx, read_xlsx_sheets, read_table

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

SHARED = ["昵称", "主页链接", "房间号", "主播甲", "主播乙", "主播丙",
          "https://www.douyin.com/user/AAA111", "https://www.douyin.com/user/BBB222",
          "https://live.douyin.com/123456789", "https://www.douyin.com/user/CCC333",
          "名称", "主播丁", "123456789"]


def _cell(ref, value, shared=False):
    if shared:
        idx = SHARED.index(value)
        return '<c r="%s" t="s"><v>%d</v></c>' % (ref, idx)
    return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, value)


def _sheet_xml(rows):
    out = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, val in enumerate(row):
            ref = chr(ord("A") + c) + str(r)
            cells.append(_cell(ref, val, shared=True))
        out.append('<row r="%d">%s</row>' % (r, "".join(cells)))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (NS, "".join(out)))


def make_workbook(path):
    sheets = [
        ("当前关注名单", [["昵称", "主页链接"], ["主播甲", SHARED[6]], ["主播乙", SHARED[7]]]),
        ("车", [["主播丙", SHARED[9]], ["主播甲", SHARED[6]]]),
        ("空表", []),
        ("房间号表", [["名称", "房间号"], ["主播丁", "123456789"]]),
    ]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets></workbook>'
                    % (NS, R, "".join(
                        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (s[0], i + 1, i + 1)
                        for i, s in enumerate(sheets))))
        rels = "".join(
            '<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>'
            % (i + 1, R, i + 1) for i in range(len(sheets)))
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>'
                    % rels)
        zf.writestr("xl/sharedStrings.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>'
                    % (NS, len(SHARED), len(SHARED),
                       "".join('<si><t>%s</t></si>' % s for s in SHARED)))
        for i, (_name, rows) in enumerate(sheets):
            zf.writestr("xl/worksheets/sheet%d.xml" % (i + 1), _sheet_xml(rows))


def test_read_sheets():
    path = TMP / "multi.xlsx"
    make_workbook(path)
    sheets = read_xlsx_sheets(str(path))
    print("sheets:", [(n, len(r)) for n, r in sheets])
    assert [n for n, _ in sheets] == ["当前关注名单", "车", "空表", "房间号表"]
    assert len(sheets[0][1]) == 3
    assert sheets[2][1] == []
    # 兼容旧接口：只读第一个 sheet
    assert read_xlsx(str(path)) == sheets[0][1]
    assert read_table(str(path)) == sheets[0][1]
    print("PASS 读取全部 sheet（顺序/空表/兼容旧接口）")


def test_records_merge():
    path = TMP / "multi.xlsx"
    records, skipped, info = import_util.records_from_file(str(path))
    print("records:", len(records), "skipped:", skipped)
    print("info:", [(i["sheet"], i["rows"], i["records"]) for i in info])
    assert len(records) == 5, records
    assert skipped == 0
    assert [i["sheet"] for i in info] == ["当前关注名单", "车", "空表", "房间号表"]
    assert info[2]["records"] == 0
    names = [r.get("name") for r in records]
    assert names[:2] == ["主播甲", "主播乙"]
    assert names[-1] == "主播丁"
    # 跨 sheet 去重
    accepted, dup, invalid = import_util.dedupe(records)
    print("accepted:", len(accepted), "dup:", dup)
    assert len(accepted) == 4, accepted
    assert dup == 1
    print("PASS 多 sheet 合并 + 每 sheet 独立表头 + 跨 sheet 去重")


def test_cli_import():
    path = TMP / "multi.xlsx"
    wl = TMP / "wl.json"
    cfg = TMP / "cfg.json"
    cfg.write_text('{"output_dir": "%s"}' % str(TMP / "out").replace("\\", "/"), encoding="utf-8")
    env = dict()
    import os
    env.update(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, str(ROOT / "run.py"),
                        "--config", str(cfg), "--watchlist", str(wl),
                        "--import-file", str(path)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=180)
    print(r.stdout[-500:])
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    assert "共 4 个 sheet" in r.stdout
    assert "新增: 4" in r.stdout, r.stdout[-800:]
    print("PASS CLI 多 sheet 导入")


def main():
    test_read_sheets()
    test_records_merge()
    test_cli_import()
    print("ALL MULTISHEET IMPORT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
