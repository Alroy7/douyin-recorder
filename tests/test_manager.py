# -*- coding: utf-8 -*-
"""监控调度测试（mock 直播源）：自动开录、并发、下播停止、停止清理。"""
import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.config import Config
from src.manager import MonitorManager
from src.mock_api import configure as mock_configure
from src.storage import Watchlist

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
LOG = logging.getLogger("test.manager")

TMP = ROOT / "tests" / "tmp"
OUT = TMP / "mgr_out"


def make_env(name, live_seconds=60, offline_after=0.0, max_concurrent=3, interval=5):
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = Config.load(TMP / ("test_config_%s.json" % name))
    cfg["output_dir"] = str(OUT)
    cfg["check_interval"] = interval
    cfg["max_concurrent"] = max_concurrent
    cfg["use_mock_api"] = True
    cfg["container"] = "flv"
    cfg["remux_mp4"] = False
    cfg["keep_days"] = 0
    events = []
    wl = Watchlist(TMP / ("watchlist_%s.json" % name))
    wl.clear()
    wl.add_from_text("111111111111", name="主播甲")
    wl.add_from_text("222222222222", name="主播乙")
    wl.add_from_text("333333333333", name="主播丙")
    mock_configure(
        ffmpeg_path=cfg.get_str("ffmpeg_path"),
        live_seconds=live_seconds,
        offline_after=offline_after,
        disconnect_after=0,
    )
    mgr = MonitorManager(cfg, wl, on_event=events.append)
    return cfg, wl, mgr, events


def wait_until(cond, timeout, step=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def test_auto_record():
    cfg, wl, mgr, events = make_env("auto")
    ok, msg = mgr.start()
    assert ok, msg
    try:
        started = wait_until(lambda: sum(1 for r in mgr.snapshot() if r["recording"]) >= 2, 40)
        snap = mgr.snapshot()
        LOG.info("快照: %s", [(r["name"], r["status"], r["recording"]) for r in snap])
        assert started, "expected recordings to start: %s" % snap
        def any_big_file():
            for f in OUT.rglob("*.flv"):
                try:
                    if f.stat().st_size > 200 * 1024:
                        return True
                except OSError:
                    pass
            return False
        big = wait_until(any_big_file, 30)
        files = list(OUT.rglob("*.flv"))
        LOG.info("输出文件: %s", [(f.name, f.stat().st_size) for f in files if f.exists()])
        assert big, "output files too small or missing"
    finally:
        mgr.stop(wait=True, timeout=40)
    assert not mgr.is_running()
    return True


def test_offline_stop():
    # mock 12 秒后下播，监控应在下一轮检查时停止录制
    cfg, wl, mgr, events = make_env("offline", live_seconds=60, offline_after=12)
    ok, msg = mgr.start()
    assert ok, msg
    try:
        assert wait_until(lambda: any(r["recording"] for r in mgr.snapshot()), 40)
        stopped = wait_until(
            lambda: any(r["is_live"] is False and not r["recording"] for r in mgr.snapshot()),
            60,
        )
        snap = mgr.snapshot()
        LOG.info("下播后快照: %s", [(r["name"], r["status"], r["recording"], r["stop_reason"]) for r in snap])
        assert stopped, "recordings did not stop after offline"
    finally:
        mgr.stop(wait=True, timeout=40)
    return True


def test_max_concurrent():
    cfg, wl, mgr, events = make_env("concurrent", max_concurrent=1)
    ok, _ = mgr.start()
    assert ok
    try:
        assert wait_until(lambda: sum(1 for r in mgr.snapshot() if r["recording"]) == 1, 40)
        time.sleep(8)
        rec_count = sum(1 for r in mgr.snapshot() if r["recording"])
        LOG.info("并发上限=1 时实际录制数: %d", rec_count)
        assert rec_count == 1, "max_concurrent not enforced: %d" % rec_count
    finally:
        mgr.stop(wait=True, timeout=40)
    return True


def main():
    failed = []
    for name, fn in (("自动开录", test_auto_record), ("下播停止", test_offline_stop),
                     ("并发上限", test_max_concurrent)):
        LOG.info("========== 开始: %s ==========", name)
        t0 = time.time()
        try:
            fn()
            LOG.info("========== 通过: %s (%.1fs) ==========", name, time.time() - t0)
        except Exception as exc:
            LOG.exception("========== 失败: %s -> %s ==========", name, exc)
            failed.append((name, str(exc)))
    if failed:
        print("FAILED:", failed)
        return 1
    print("ALL MANAGER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())