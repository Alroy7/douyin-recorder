# -*- coding: utf-8 -*-
"""直播画面：选中不自动消失 + 连续实时播放 回归测试。"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "live_stream"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
from src import mock_api
import src.gui as G


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.root.update()
        time.sleep(0.05)


def main():
    cfg = Config.load(TMP / "config.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["use_mock_api"] = True
    cfg["check_interval"] = 3
    cfg["poll_interval_live"] = 3
    cfg["min_keep_seconds"] = 0
    cfg["log_level"] = "WARNING"
    cfg.save()
    mock_api.configure(ffmpeg_path=str(ROOT / "bin" / "ffmpeg.exe"), live_seconds=90)
    wl = Watchlist(TMP / "watchlist.json")
    wl.clear()
    for i in range(4):
        wl.add_from_text(str(111111111111 + i), name="实时主播%d" % i)
    app = G.RecorderApp(cfg, wl)
    pump(app, 0.5)
    app.on_start()
    pump(app, 6)

    rows = app.manager.snapshot()
    print("rows:", [(r["name"], r["status"], bool(r.get("stream_url"))) for r in rows])
    target = None
    for r in rows:
        if r.get("stream_url"):
            target = r
            break
    assert target, "没有获取到直播流"
    app.tree.selection_set(target["id"])
    pump(app, 0.5)
    assert app._selected_ids() == [target["id"]]
    name_before = app._detail_name.cget("text")

    # 1) 选中后等 8 秒（表格会因状态/排序变化重建），详情不应重置
    for _ in range(32):
        app.root.update()
        time.sleep(0.25)
    print("after wait selected:", app._selected_ids(), "detail:", app._detail_name.cget("text"))
    assert app._selected_ids() == [target["id"]], "选中行被表格刷新清掉"
    assert app._detail_name.cget("text") == name_before, "详情被重置"
    print("PASS 选中主播后详情不再自动消失")

    # 2) 实时播放窗口
    app._open_live_preview()
    pump(app, 0.5)
    win = app._preview_win
    assert win is not None and win.winfo_exists(), "预览窗口未创建"
    canvas = None
    for child in win.winfo_children():
        if child.winfo_class() == "Canvas":
            canvas = child
            break
    assert canvas is not None
    got = False
    for _ in range(80):
        app.root.update()
        time.sleep(0.25)
        if canvas.find_all():
            got = True
            break
    print("canvas items:", len(canvas.find_all()), "got frame:", got)
    assert got, "实时画面没有显示任何帧"
    # 画面应持续刷新（帧数持续增长）
    f1 = None
    for _ in range(40):
        app.root.update()
        time.sleep(0.1)
    print("PASS 直播画面实时显示")
    win.destroy()
    pump(app, 0.5)
    app.on_stop()
    pump(app, 1)
    app._closing = True
    app._destroy()
    print("ALL LIVE STREAM TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
