# -*- coding: utf-8 -*-
"""主播列表导出/导入往返测试（xlsx/csv/CLI/GUI）。"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "export_test"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.storage import Watchlist
from src import export_util, import_util
from src.model import WatchItem


def build_watchlist(path):
    wl = Watchlist(path)
    wl.clear()
    item = wl.add_from_text("https://live.douyin.com/123456789", name="张三", save=False)
    item.group = "家具"
    item.quality = "hd"
    item.auto_record = False
    item.output_template = "{anchor}/{date}"
    item.note = "备注A"
    item2 = wl.add_from_text("https://www.douyin.com/user/MS4wLjABAAAA_demo", name="李四", save=False)
    item2.group = "测试"
    item2.quality = "best"
    item3 = wl.add_from_text("只昵称主播", name="只昵称主播", save=False)
    item3.enabled = False
    item4 = wl.add_from_text("987654321", name="王五", save=False)
    # 只有昵称、没有名字也没有链接的条目（真实场景：旧列表遗留），导出应标记为停用
    wl.add_from_text("only_nickname_demo", name="", save=False)
    wl.save()
    return wl


def apply_records(wl, records):
    added = 0
    for rec in records:
        auto = import_util.parse_bool(rec.get("auto_record"), True)
        quality = import_util.parse_quality(rec.get("quality"), "origin")
        item = wl.add_from_text(rec["target"], name=rec.get("name") or "",
                                auto_record=auto, quality=quality,
                                output_template=rec.get("output_template") or "", save=False)
        if item is None:
            continue
        if rec.get("web_rid") and not item.web_rid:
            item.web_rid = rec["web_rid"]
        if rec.get("group"):
            item.group = rec["group"]
        if "enabled" in rec:
            item.enabled = import_util.parse_bool(rec.get("enabled"), True)
        if rec.get("note"):
            item.note = rec["note"]
        added += 1
    wl.save()
    return added


def test_xlsx_roundtrip():
    src_path = TMP / "wl_src.json"
    wl = build_watchlist(src_path)
    out = TMP / "主播列表.xlsx"
    n = export_util.export_watchlist(wl, str(out))
    assert n == 5, n
    rows = import_util.read_table(str(out))
    assert len(rows) == 6, len(rows)  # 表头 + 5 行
    records, skipped = import_util.records_from_table(rows)
    assert skipped == 0, skipped
    assert len(records) == 5, records
    by_name = {r["name"]: r for r in records}
    assert by_name["张三"]["web_rid"] == "123456789"
    assert by_name["张三"]["group"] == "家具"
    assert by_name["张三"]["quality"] == "hd"
    assert by_name["张三"]["auto_record"] == "否"
    assert by_name["张三"]["output_template"] == "{anchor}/{date}"
    assert by_name["张三"]["note"] == "备注A"
    assert by_name["李四"]["homepage"].endswith("MS4wLjABAAAA_demo")
    assert by_name["只昵称主播"]["enabled"] == "否"
    only = [r for r in records if r["target"] == "only_nickname_demo"]
    assert only and only[0]["enabled"] == "否" and only[0]["name"] == "only_nickname_demo"
    # 导入到新列表
    dst = Watchlist(TMP / "wl_dst.json")
    dst.clear()
    added = apply_records(dst, records)
    assert added == 5
    got = {it.name: it for it in dst.all()}
    assert got["张三"].web_rid == "123456789"
    assert got["张三"].group == "家具"
    assert got["张三"].quality == "hd"
    assert got["张三"].auto_record is False
    assert got["张三"].output_template == "{anchor}/{date}"
    assert got["张三"].note == "备注A"
    assert got["李四"].url.endswith("MS4wLjABAAAA_demo")
    assert got["只昵称主播"].enabled is False
    assert got["only_nickname_demo"].enabled is False
    print("PASS xlsx 导出/导入往返（5 条字段完整，含仅昵称停用）")


def test_csv_roundtrip():
    wl = Watchlist(TMP / "wl_src.json")
    out = TMP / "主播列表.csv"
    export_util.export_watchlist(wl, str(out))
    rows = import_util.read_table(str(out))
    records, skipped = import_util.records_from_table(rows)
    assert skipped == 0 and len(records) == 5, (skipped, records)
    print("PASS csv 导出/导入往返")


def test_cli():
    src = TMP / "cli_src.json"
    build_watchlist(src)
    out = TMP / "cli_export.xlsx"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, str(ROOT / "run.py"), "--watchlist", str(src),
                        "--export-watchlist", str(out)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=120)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "已导出 5 个主播" in r.stdout, r.stdout
    assert out.exists() and out.stat().st_size > 500
    dst = TMP / "cli_dst.json"
    r2 = subprocess.run([sys.executable, str(ROOT / "run.py"), "--watchlist", str(dst),
                         "--import-file", str(out)],
                        cwd=str(ROOT), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env=env, timeout=120)
    assert r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr)
    wl = Watchlist(dst)
    assert len(wl) == 5, (len(wl), r2.stdout)
    names = {it.name: it for it in wl.all()}
    assert names["张三"].group == "家具" and names["张三"].quality == "hd"
    assert names["只昵称主播"].enabled is False
    print("PASS CLI 导出/导入往返")


def test_gui_button():
    os.environ.setdefault("DYREC_HISTORY_DB", str(TMP / "history.db"))
    from src.config import Config
    import src.gui as G
    cfg = Config.load(TMP / "gui_config.json")
    cfg["output_dir"] = str(TMP / "out")
    wl = Watchlist(TMP / "wl_src.json")
    app = G.RecorderApp(cfg, wl)
    for _ in range(30):
        app.root.update(); time.sleep(0.01)
    assert hasattr(app, "_btn_export"), "导出按钮未创建"
    out = TMP / "gui_export.xlsx"
    orig_save = G.filedialog.asksaveasfilename
    orig_info = G.messagebox.showinfo
    G.filedialog.asksaveasfilename = lambda **kw: str(out)
    G.messagebox.showinfo = lambda *a, **kw: None
    try:
        app.export_watchlist()
    finally:
        G.filedialog.asksaveasfilename = orig_save
        G.messagebox.showinfo = orig_info
    assert out.exists(), "GUI 导出文件不存在"
    rows = import_util.read_table(str(out))
    assert len(rows) == 6, len(rows)
    app._closing = True
    app._destroy()
    print("PASS GUI 导出按钮")


def main():
    test_xlsx_roundtrip()
    test_csv_roundtrip()
    test_cli()
    test_gui_button()
    print("ALL EXPORT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
