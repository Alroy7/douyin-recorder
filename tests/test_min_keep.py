# -*- coding: utf-8 -*-
"""最短保留时长自动删除测试（任务 A）。

覆盖：
  1. min_keep_seconds=30 录约 4 秒 -> 本场产物（含 sidecar）全部删除，状态 stopped，
     记录 INFO 事件「过短录制已删除」，STOPPED 事件带 deleted=True，历史库 deleted=1；
  2. min_keep_seconds=0 关闭 -> 录制文件保留；
  3. 时长超过阈值 -> 保留（证明不是无条件删除）；
  4. remux 成 MP4 后仍按总时长判断并全删；
  5. ffmpeg 解析时长失败时回落运行时计时；ffmpeg 仍在写入时保护不删。

运行：python tests/test_min_keep.py
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

# 测试使用独立历史库，避免污染真实 data/history.db
os.environ["DYREC_HISTORY_DB"] = str(ROOT / "tests" / "tmp" / "minkeep_history.db")

from src.config import Config
from src import ffmpeg_util
from src import history
from src.mock_api import configure as mock_configure
from src.checker import LiveChecker
from src.recorder import RoomRecorder
from src.model import EventType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("test.min_keep")

OUT = ROOT / "tests" / "tmp" / "minkeep_out"
CFG = Config.load(ROOT / "tests" / "tmp" / "test_config.json")
FFMPEG = ffmpeg_util.resolve_ffmpeg(CFG.get_str("ffmpeg_path"))[0]
assert FFMPEG, "测试需要可用的 ffmpeg（bin/ffmpeg.exe）"


def reset_out():
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)


def make_url():
    mock_configure(ffmpeg_path=FFMPEG, live_seconds=60, offline_after=0, disconnect_after=0)
    checker = LiveChecker(CFG, use_mock=True)
    st = checker.check("111111111111")
    assert st.is_live, st.error
    url = checker.choose_url(st)
    assert url, "mock 未提供可录制流地址"
    return url


def record_seconds(seconds, base, **options):
    """录制 seconds 秒后停止，返回 (recorder, 事件列表)。"""
    reset_out()
    url = make_url()
    events = []
    kwargs = dict(
        name="最短时长测试", room_id="111111111111",
        remux_mp4=False, retry_max=2, retry_interval=1,
        on_event=events.append,
    )
    kwargs.update(options)
    rec = RoomRecorder(FFMPEG, url, OUT, base, **kwargs)
    assert rec.start(), "start failed"
    time.sleep(seconds)
    # 模拟 remux 收尾用的 sidecar/临时文件：过短删除时也应一并清掉
    (OUT / (".%s_concat.txt" % base)).write_text("sidecar", encoding="utf-8")
    rec.stop("测试停止")
    assert rec.join(90), "recorder thread did not exit"
    return rec, events


def dir_files():
    return sorted(p.name for p in OUT.iterdir()) if OUT.exists() else []


def test_delete_when_too_short():
    rec, events = record_seconds(4, "too_short", min_keep_seconds=30)
    stt = rec.status()
    LOG.info("过短录制: state=%s deleted=%s files=%s", stt["state"], stt["deleted"], stt["files"])
    assert stt["state"] == "stopped", stt
    assert stt["deleted"] is True, stt
    assert stt["files"] == [], stt["files"]
    assert dir_files() == [], "过短录制产物未删净: %s" % dir_files()
    info_events = [e for e in events
                   if e.kind == EventType.INFO and "过短录制已删除" in e.message]
    assert info_events, [e.message for e in events]
    assert "30" in info_events[-1].message
    stopped = [e for e in events if e.kind == EventType.STOPPED]
    assert stopped, "缺少 STOPPED 事件"
    assert "已自动删除" in stopped[-1].message, stopped[-1].message
    assert getattr(stopped[-1], "deleted", False) is True
    rows = [r for r in history.list_recent_recordings(days=1)
            if r.get("anchor") == "最短时长测试"]
    assert rows, "历史库没有录制记录"
    assert rows[0]["deleted"] is True, rows[0]
    return True


def test_keep_when_disabled():
    rec, events = record_seconds(4, "keep_disabled", min_keep_seconds=0)
    stt = rec.status()
    files = [Path(p) for p in stt["files"]]
    LOG.info("关闭阈值: files=%s", [f.name for f in files])
    assert files, "min_keep_seconds=0 时不应删除产物"
    assert all(f.exists() and f.stat().st_size > 0 for f in files)
    assert stt["deleted"] is False
    assert not [e for e in events if "过短录制已删除" in e.message]
    return True


def test_keep_when_long_enough():
    # 录约 5 秒，阈值 2 秒：解析出的时长应超过阈值，文件保留
    rec, _events = record_seconds(5, "keep_long", min_keep_seconds=2)
    stt = rec.status()
    LOG.info("超过阈值: files=%s deleted=%s", [Path(p).name for p in stt["files"]], stt["deleted"])
    assert stt["deleted"] is False
    assert stt["files"], "超过阈值的录制不应被删除"
    return True


def test_remux_then_delete():
    rec, _events = record_seconds(4, "remux_short", min_keep_seconds=30, remux_mp4=True)
    stt = rec.status()
    LOG.info("remux 后删除: files=%s deleted=%s", stt["files"], stt["deleted"])
    assert stt["deleted"] is True, stt
    assert stt["files"] == []
    assert dir_files() == [], "remux 后仍有残留: %s" % dir_files()
    return True


def test_duration_fallback_and_running_protection():
    """用不存在的 ffmpeg 模拟「解析时长失败」，验证回落运行时计时；并验证写入保护。"""
    reset_out()
    dummy = OUT / "fallback_000.flv"
    dummy.write_bytes(b"x" * 2048)
    rec = RoomRecorder(str(ROOT / "tests" / "tmp" / "no_such_ffmpeg.exe"),
                       "http://127.0.0.1:1/live.flv", OUT, "fallback",
                       name="回落测试", room_id="fallback",
                       remux_mp4=False, min_keep_seconds=30)
    now = time.time()
    rec._started_at = now - 5
    rec._ended_at = now
    rec._refresh_files()
    files = list(rec._files)
    assert files, "未发现测试文件"
    remaining, deleted, duration = rec._delete_if_too_short(files)
    LOG.info("解析失败回落: duration=%.1f deleted=%s", duration, deleted)
    assert deleted and not remaining, (remaining, deleted)
    assert not dummy.exists(), "过短文件应被删除"

    # 正在写入保护
    reset_out()
    running = OUT / "running_000.flv"
    running.write_bytes(b"y" * 2048)
    rec2 = RoomRecorder(str(ROOT / "tests" / "tmp" / "no_such_ffmpeg.exe"),
                        "http://127.0.0.1:1/live.flv", OUT, "running",
                        name="写入保护测试", room_id="running",
                        remux_mp4=False, min_keep_seconds=30)
    rec2._started_at = now - 5
    rec2._ended_at = now

    class _FakeProc:
        def poll(self):
            return None  # 模拟 ffmpeg 仍在运行

    rec2._proc = _FakeProc()
    rec2._refresh_files()
    remaining2, deleted2, _duration2 = rec2._delete_if_too_short(list(rec2._files))
    rec2._proc = None
    LOG.info("写入保护: remaining=%s deleted=%s", [p.name for p in remaining2], deleted2)
    assert not deleted2, "ffmpeg 仍在写入时不应删除"
    assert remaining2 and running.exists()
    return True


TESTS = [
    ("过短自动删除", test_delete_when_too_short),
    ("关闭阈值保留", test_keep_when_disabled),
    ("超过阈值保留", test_keep_when_long_enough),
    ("remux 后删除", test_remux_then_delete),
    ("解析回落/写入保护", test_duration_fallback_and_running_protection),
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
    history.close_all()
    print()
    if failed:
        print("FAILED:")
        for name, exc in failed:
            print("  -", name, ":", exc)
        return 1
    print("ALL MIN_KEEP TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
