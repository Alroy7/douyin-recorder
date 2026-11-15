# -*- coding: utf-8 -*-
"""停止监控/关闭软件时，必须等待录制收尾并转成 MP4。"""
import shutil, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "shutdown_remux"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
from src.manager import MonitorManager
from src import mock_api


def make_cfg(name):
    cfg = Config.load(TMP / ("config_%s.json" % name))
    cfg["output_dir"] = str(TMP / name / "out")
    cfg["use_mock_api"] = True
    cfg["check_interval"] = 3
    cfg["poll_interval_live"] = 3
    cfg["poll_interval_warm"] = 3
    cfg["poll_interval_cold"] = 3
    cfg["container"] = "flv"
    cfg["remux_mp4"] = True
    cfg["keep_source"] = False
    cfg["min_keep_seconds"] = 0
    cfg["record_timeout_min"] = 0
    cfg["log_level"] = "WARNING"
    cfg.save()
    wl = Watchlist(TMP / name / "watchlist.json")
    wl.clear()
    wl.add_from_text("111111111111", name="收尾主播")
    return cfg, wl


def wait_recording(mgr, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        snaps = mgr.snapshot()
        if snaps and snaps[0].get("recording"):
            return True
        time.sleep(0.3)
    return False


def test_manager_stop_waits():
    cfg, wl = make_cfg("mgr")
    mock_api.configure(ffmpeg_path=str(ROOT / "bin" / "ffmpeg.exe"), live_seconds=60)
    mgr = MonitorManager(cfg, wl)
    ok, msg = mgr.start()
    assert ok, msg
    assert wait_recording(mgr), "未开始录制"
    print("recording started, pending=", mgr.pending_count())
    time.sleep(4)
    t0 = time.time()
    done = mgr.stop(wait=True, timeout=120)
    cost = time.time() - t0
    print("stop done=%s cost=%.1fs" % (done, cost))
    assert done, "stop 未等待收尾完成"
    out = Path(cfg["output_dir"])
    files = [p for p in out.rglob("*") if p.is_file()]
    print("files:", [p.name for p in files])
    mp4 = [p for p in files if p.suffix.lower() == ".mp4"]
    src = [p for p in files if p.suffix.lower() in (".flv", ".ts")]
    assert mp4, "停止后没有生成 MP4"
    assert not src, "停止后仍残留 FLV/TS"
    print("PASS 停止监控会等待转 MP4 完成")


def test_gui_close_waits():
    import src.gui as G
    cfg, wl = make_cfg("gui")
    mock_api.configure(ffmpeg_path=str(ROOT / "bin" / "ffmpeg.exe"), live_seconds=60)
    app = G.RecorderApp(cfg, wl)
    for _ in range(20):
        app.root.update(); time.sleep(0.01)
    app.on_start()
    for _ in range(60):
        app.root.update(); time.sleep(0.25)
        if app.manager.pending_count() > 0:
            break
    assert app.manager.pending_count() > 0, "GUI 未开始录制"
    time.sleep(4)
    finished = {"flag": False}
    orig_destroy = app._destroy
    app._destroy = lambda: finished.__setitem__("flag", True)
    seen = {"dialog": False}
    app.on_close()
    t0 = time.time()
    for _ in range(240):
        app.root.update(); time.sleep(0.25)
        if app._closing_dialog is not None:
            seen["dialog"] = True
        if finished["flag"]:
            break
    print("gui close finished=%s dialog=%s cost=%.1fs" % (finished["flag"], seen["dialog"], time.time()-t0))
    assert finished["flag"], "关闭流程未完成"
    assert seen["dialog"], "没有显示退出等待进度窗口"
    files = [p for p in Path(cfg["output_dir"]).rglob("*") if p.is_file()]
    mp4 = [p for p in files if p.suffix.lower() == ".mp4"]
    src = [p for p in files if p.suffix.lower() in (".flv", ".ts")]
    print("files:", [p.name for p in files])
    assert mp4, "关闭后没有 MP4"
    assert not src, "关闭后仍残留 FLV/TS"
    app._destroy = orig_destroy
    app._closing = True
    try:
        app.root.destroy()
    except Exception:
        pass
    print("PASS 关闭软件会等待转 MP4 完成")


def main():
    test_manager_stop_waits()
    test_gui_close_waits()
    print("ALL SHUTDOWN REMUX TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
