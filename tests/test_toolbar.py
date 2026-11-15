# -*- coding: utf-8 -*-
"""监控页工具栏回归：导入/添加/导出按钮在顶部工具栏可见，空列表有引导提示。"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "toolbar_test"
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
    app.root.geometry("1255x853")
    for _ in range(40):
        app.root.update(); time.sleep(0.01)
    return app, wl


def test_buttons_visible():
    app, wl = make_app()
    for name in ("_btn_batch_import", "_btn_add", "_btn_export", "_btn_refresh",
                 "_btn_batch_rec", "_btn_batch_set", "_btn_detail"):
        w = getattr(app, name)
        assert w.winfo_ismapped(), "%s 不可见" % name
        assert w.winfo_width() >= 70, "%s 被压缩（%d px）" % (name, w.winfo_width())
    imp = app._btn_batch_import
    assert imp.winfo_y() < 140, "导入按钮不在顶部工具栏"
    assert imp.winfo_x() < 400, "导入按钮不在工具栏左侧"
    # 关键：按钮必须以工具栏/头部的子帧为父控件，否则会被后创建的帧覆盖（Windows 上“消失”）
    assert imp.master is app._btn_refresh.master, "导入按钮父容器错误"
    assert app._btn_add.master is app._btn_refresh.master
    assert app._btn_export.master is app._btn_refresh.master
    assert app._btn_start.master is app._btn_stop.master, "开始/停止按钮父容器不一致"
    assert app._btn_start.master is not app._page_monitor, "开始按钮不应以整页为父控件（会被覆盖）"
    print("按钮位置:", {n: (getattr(app, n).winfo_x(), getattr(app, n).winfo_y())
                       for n in ("_btn_batch_import", "_btn_add", "_btn_export")})
    app._closing = True; app._destroy()
    print("PASS 导入/添加/导出按钮在顶部工具栏可见")


def test_empty_hint():
    app, wl = make_app()
    app._refresh_table(force=True)
    app.root.update()
    assert app._empty_hint.winfo_ismapped(), "空列表没有引导提示"
    print("空列表提示:", app._empty_hint.cget("text").replace("\n", " | "))
    wl.add_from_text("111111111111", name="主播A", save=True)
    app._refresh_table(force=True)
    app.root.update()
    assert not app._empty_hint.winfo_ismapped(), "有数据时引导提示应隐藏"
    app._closing = True; app._destroy()
    print("PASS 空列表提示显示/隐藏正确")


def main():
    test_buttons_visible()
    test_empty_hint()
    print("ALL TOOLBAR TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
