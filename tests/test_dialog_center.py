# -*- coding: utf-8 -*-
"""子窗口居中测试：添加/导入/批量设置/退出对话框应显示在父窗口中间，而不是屏幕左上角。"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "dialog_center"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
import src.gui as G


def make_app():
    cfg = Config.load(TMP / "config.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["log_level"] = "WARNING"
    cfg.save()
    wl = Watchlist(TMP / "watchlist.json")
    wl.clear()
    app = G.RecorderApp(cfg, wl)
    app.root.geometry("1000x700+180+140")
    for _ in range(40):
        app.root.update(); time.sleep(0.01)
    return app


def center_of(widget):
    widget.update_idletasks()
    return (widget.winfo_rootx() + widget.winfo_width() // 2,
            widget.winfo_rooty() + widget.winfo_height() // 2)


def check_dialog(app, dlg, name):
    for _ in range(20):
        app.root.update(); time.sleep(0.02)
    rc = center_of(app.root)
    dc = center_of(dlg)
    print("%s: root_center=%s dialog_center=%s" % (name, rc, dc))
    assert abs(rc[0] - dc[0]) < 120, "%s 水平没有居中: %s vs %s" % (name, rc, dc)
    assert abs(rc[1] - dc[1]) < 160, "%s 垂直没有居中: %s vs %s" % (name, rc, dc)
    assert dc[0] > 5 and dc[1] > 5, "%s 跑到屏幕左上角: %s" % (name, dc)
    dlg.destroy()
    for _ in range(5):
        app.root.update()


def main():
    app = make_app()
    dlg = G.ItemDialog(app.root)
    check_dialog(app, dlg, "添加主播对话框")

    dlg = G.BatchImportDialog(app.root)
    check_dialog(app, dlg, "导入直播间对话框")

    dlg = G.BatchSetDialog(app.root, 3)
    check_dialog(app, dlg, "批量设置对话框")

    app._show_closing_dialog()
    win = app._closing_dialog
    for _ in range(20):
        app.root.update(); time.sleep(0.02)
    rc = center_of(app.root)
    dc = center_of(win)
    print("正在退出: root_center=%s dialog_center=%s" % (rc, dc))
    assert abs(rc[0] - dc[0]) < 120
    assert abs(rc[1] - dc[1]) < 160
    app._hide_closing_dialog()
    app._closing = True
    app._destroy()
    print("ALL DIALOG CENTER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
