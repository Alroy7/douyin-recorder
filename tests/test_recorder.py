# -*- coding: utf-8 -*-
"""录制引擎机制测试（不依赖真实抖音流）。

覆盖：
  1. 基础录制 + 手动停止 -> 生成可播放 FLV；
  2. 断流自动重连（mock 服务器每 N 秒断开）-> 多个文件、重连计数；
  3. 分段录制（-f segment）-> 多个 _000/_001 文件；
  4. 下播自动停止（is_live_fn 返回 False）；
  5. remux 为 MP4；
  6. 连续失败退避与收尾。

运行：python tests/test_recorder.py
"""
import logging
import os
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
from src import ffmpeg_util
from src.mock_api import configure as mock_configure
from src.checker import LiveChecker
from src.recorder import RoomRecorder
from src.model import EventType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("test.recorder")

OUT = ROOT / "tests" / "tmp" / "rec_out"
CFG = Config.load(ROOT / "tests" / "tmp" / "test_config.json")
FFMPEG = ffmpeg_util.resolve_ffmpeg(CFG.get_str("ffmpeg_path"))[0]


def reset_out():
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)


def make_checker(live_seconds=60, offline_after=0.0, disconnect_after=0.0):
    mock_configure(
        ffmpeg_path=FFMPEG,
        live_seconds=live_seconds,
        offline_after=offline_after,
        disconnect_after=disconnect_after,
    )
    return LiveChecker(CFG, use_mock=True)


def collect_events():
    events = []
    def cb(ev):
        events.append(ev)
    return events, cb


def test_basic():
    reset_out()
    checker = make_checker(live_seconds=60)
    st = checker.check("111111111111")
    url = checker.choose_url(st)
    assert url, "no url"
    events, cb = collect_events()
    rec = RoomRecorder(
        FFMPEG, url, OUT, "basic_test",
        name="基础测试", room_id="111111111111",
        remux_mp4=False, retry_max=2, retry_interval=1, on_event=cb,
    )
    assert rec.start(), "start failed"
    time.sleep(10)
    rec.stop("测试停止")
    assert rec.join(30), "recorder thread did not exit"
    stt = rec.status()
    files = [Path(p) for p in stt["files"]]
    assert files, "no files produced"
    size = sum(f.stat().st_size for f in files if f.exists())
    LOG.info("基础录制: files=%s size=%d duration=%d", [f.name for f in files], size, stt["duration"])
    assert size > 100 * 1024, "file too small: %d" % size
    assert stt["state"] == "stopped", stt["state"]
    assert any(e.kind == EventType.START for e in events), "no START event"
    assert any(e.kind == EventType.STOPPED for e in events), "no STOPPED event"
    return True


def test_reconnect():
    reset_out()
    checker = make_checker(live_seconds=60, disconnect_after=4)
    st = checker.check("222222222222")
    url = checker.choose_url(st)
    events, cb = collect_events()
    rec = RoomRecorder(
        FFMPEG, url, OUT, "reconnect_test",
        name="断流测试", room_id="222222222222",
        remux_mp4=False, http_input_args=[],
        retry_max=5, retry_interval=1, retry_interval_max=3, on_event=cb,
    )
    rec.start()
    time.sleep(14)
    rec.stop("测试停止")
    assert rec.join(30)
    stt = rec.status()
    files = [Path(p) for p in stt["files"]]
    LOG.info("断流重连: files=%s reconnects=%d", [f.name for f in files], stt["reconnects"])
    assert stt["reconnects"] >= 1, "expected at least one reconnect"
    assert len(files) >= 2, "expected multiple files, got %d" % len(files)
    assert any(e.kind == EventType.RECONNECT for e in events)
    return True


def test_segment():
    reset_out()
    checker = make_checker(live_seconds=60)
    st = checker.check("333333333333")
    url = checker.choose_url(st)
    rec = RoomRecorder(
        FFMPEG, url, OUT, "segment_test",
        name="分段测试", room_id="333333333333",
        remux_mp4=False, segment_duration=3, retry_max=2, retry_interval=1,
    )
    rec.start()
    time.sleep(11)
    rec.stop("测试停止")
    assert rec.join(30)
    stt = rec.status()
    files = sorted(Path(p) for p in stt["files"])
    LOG.info("分段: %s", [f.name for f in files])
    assert len(files) >= 2, "expected >=2 segments, got %d" % len(files)
    assert any("_000.flv" in f.name for f in files), "missing _000 file"
    return True


def test_offline_stop():
    reset_out()
    checker = make_checker(live_seconds=60)
    st = checker.check("444444444444")
    url = checker.choose_url(st)
    # 5 秒后 mock 报告下播
    mock_configure(offline_after=5)
    checker2 = make_checker(live_seconds=60, offline_after=5)
    rec = RoomRecorder(
        FFMPEG, url, OUT, "offline_test",
        name="下播测试", room_id="444444444444",
        remux_mp4=False, retry_max=3, retry_interval=1,
        is_live_fn=lambda: checker2.check("444444444444").is_live,
    )
    rec.start()
    ok = rec.join(60)
    stt = rec.status()
    LOG.info("下播: state=%s reason=%s duration=%d", stt["state"], stt["stop_reason"], stt["duration"])
    assert ok, "recorder did not stop after offline"
    assert "下播" in stt["stop_reason"], stt["stop_reason"]
    return True


def test_mp4_remux():
    reset_out()
    checker = make_checker(live_seconds=60)
    st = checker.check("555555555555")
    url = checker.choose_url(st)
    rec = RoomRecorder(
        FFMPEG, url, OUT, "mp4_test",
        name="MP4测试", room_id="555555555555",
        remux_mp4=True, keep_source=False, retry_max=2, retry_interval=1,
    )
    rec.start()
    time.sleep(8)
    rec.stop("测试停止")
    assert rec.join(120), "join timeout"
    stt = rec.status()
    files = [Path(p) for p in stt["files"]]
    LOG.info("MP4: %s", [f.name for f in files])
    mp4 = [f for f in files if f.suffix.lower() == ".mp4"]
    assert mp4, "no mp4 produced: %s" % [f.name for f in files]
    assert mp4[0].stat().st_size > 100 * 1024
    return True


def test_giveup():
    reset_out()
    # 指向一个不存在的流地址，验证连续失败后收尾
    rec = RoomRecorder(
        FFMPEG, "http://127.0.0.1:1/nope.flv", OUT, "fail_test",
        name="失败测试", room_id="fail",
        remux_mp4=False, retry_max=1, retry_interval=1, retry_interval_max=2,
        http_input_args=[],                      # 关闭 ffmpeg 内部重连，快速失败
        stall_check_seconds=6, stall_kill_after=2,
    )
    rec.start()
    assert rec.join(120), "giveup recorder did not stop"
    stt = rec.status()
    LOG.info("失败收尾: state=%s reason=%s error=%s", stt["state"], stt["stop_reason"], stt["error"])
    assert stt["state"] in ("stopped", "error")
    assert stt["stop_reason"], "missing stop reason"
    return True


def test_stall_watchdog_healthy():
    """回归：健康录制不能被卡死看门狗误杀。

    背景：_total_size() 曾被写成 staticmethod 且实现为 `for f in files or []`，
    无参调用永远返回 0，导致看门狗每 30 秒误判「零数据」并重启健康录制。
    """
    reset_out()
    checker = make_checker(live_seconds=60)
    st = checker.check("333333333333")
    url = checker.choose_url(st)
    assert url, "no url"
    events, cb = collect_events()
    rec = RoomRecorder(
        FFMPEG, url, OUT, "stall_healthy_test",
        name="看门狗健康测试", room_id="333333333333",
        remux_mp4=False, retry_max=1, retry_interval=1,
        stall_check_seconds=3, stall_kill_after=2,
        on_event=cb,
    )
    assert rec.start(), "start failed"
    time.sleep(12)
    stt = rec.status()
    msgs = " ".join(str(getattr(ev, "message", "")) for ev in events)
    LOG.info("看门狗测试: state=%s file_size=%s 事件=%d", stt["state"], stt["file_size"], len(events))
    assert stt["state"] == "recording", "健康录制被看门狗停止: %s" % stt["state"]
    assert "未收到任何数据" not in msgs, "健康录制被误判为无数据: %s" % msgs
    assert "无增长" not in msgs, "健康录制被误判为文件无增长: %s" % msgs
    assert stt["file_size"] > 100 * 1024, "录制数据过小: %s" % stt["file_size"]
    rec.stop("测试停止")
    assert rec.join(30), "recorder did not stop"
    return True


TESTS = [
    ("基础录制", test_basic),
    ("看门狗健康", test_stall_watchdog_healthy),
    ("断流重连", test_reconnect),
    ("分段录制", test_segment),
    ("下播停止", test_offline_stop),
    ("MP4 remux", test_mp4_remux),
    ("失败收尾", test_giveup),
]


def main():
    failed = []
    for name, fn in TESTS:
        LOG.info("========== 开始: %s ==========", name)
        t0 = time.time()
        try:
            fn()
            LOG.info("========== 通过: %s (%.1fs) ==========", name, time.time() - t0)
        except Exception as exc:
            LOG.exception("========== 失败: %s -> %s ==========", name, exc)
            failed.append((name, str(exc)))
    print()
    if failed:
        print("FAILED:")
        for n, e in failed:
            print("  -", n, ":", e)
        return 1
    print("ALL RECORDER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())