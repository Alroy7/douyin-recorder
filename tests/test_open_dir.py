# -*- coding: utf-8 -*-
"""右键「打开目录」回归：必须打开主播的录像目录，而不是软件目录。"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "open_dir"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
from src.model import render_output_dir
import src.gui as G


def make_app(template):
    cfg = Config.load(TMP / ("config_%s.json" % abs(hash(template))))
    cfg["output_dir"] = str(TMP / "直播")
    cfg["output_template"] = template
    cfg["log_level"] = "WARNING"
    cfg.save()
    wl = Watchlist(TMP / "watchlist.json")
    wl.clear()
    wl.add_from_text("111111111111", name="目录主播")
    app = G.RecorderApp(cfg, wl)
    for _ in range(25):
        app.root.update(); time.sleep(0.01)
    return app


def test_item_dir_not_software():
    app = make_app("{anchor}/{date}")
    item = app.watchlist.all()[0]
    d = app._item_dir(item.id)
    root = app.cfg.output_root
    print("item_dir:", d)
    assert d, "_item_dir 返回空（会退化成软件目录）"
    assert Path(d).is_absolute()
    assert str(Path(d)).startswith(str(root)), d
    assert "目录主播" in d
    app._closing = True; app._destroy()
    print("PASS 主播目录计算正确（非软件目录）")


def test_open_dir_opens_anchor_dir():
    app = make_app("{anchor}/{date}")
    item = app.watchlist.all()[0]
    anchor_dir = app.cfg.output_root / "目录主播"
    anchor_dir.mkdir(parents=True, exist_ok=True)
    opened = {}
    orig = G.os.startfile
    G.os.startfile = lambda p: opened.setdefault("path", p)
    try:
        app.tree.selection_set(item.id); app.root.update()
        app.on_open_dir()
    finally:
        G.os.startfile = orig
    print("opened:", opened.get("path"))
    assert opened, "没有调用打开目录"
    p = Path(opened["path"])
    assert p == anchor_dir, (p, anchor_dir)
    # 最近文件也能从主播目录读到
    f = anchor_dir / "目录主播_2026-11-14_160000.flv"
    f.write_bytes(b"x" * 1024)
    txt = app._recent_files_text(str(anchor_dir))
    assert "目录主播_2026-11-14_160000.flv" in txt, txt
    app._closing = True; app._destroy()
    print("PASS 打开的是主播录像目录，最近文件可读")


def test_no_selection():
    app = make_app("{anchor}")
    info = {}
    orig = G.messagebox.showinfo
    G.messagebox.showinfo = lambda *a, **kw: info.setdefault("called", True)
    try:
        app.tree.selection_set(())
        app.on_open_dir()
    finally:
        G.messagebox.showinfo = orig
    assert info.get("called"), "未选择主播时应提示而不是打开软件目录"
    app._closing = True; app._destroy()
    print("PASS 未选择主播时提示，不误开目录")


def test_log_dir():
    app = make_app("{anchor}")
    expected = app.cfg.log_dir_path
    got = app._log_dir()
    print("log_dir:", got)
    assert got == expected, (got, expected)
    opened = {}
    orig = G.os.startfile
    G.os.startfile = lambda p: opened.setdefault("path", p)
    try:
        app._open_log_dir()
    finally:
        G.os.startfile = orig
    assert Path(opened.get("path")) == expected, (opened, expected)
    app._closing = True; app._destroy()
    print("PASS 打开日志目录正确（非软件目录）")


def main():
    test_item_dir_not_software()
    test_open_dir_opens_anchor_dir()
    test_no_selection()
    test_log_dir()
    print("ALL OPEN_DIR TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
