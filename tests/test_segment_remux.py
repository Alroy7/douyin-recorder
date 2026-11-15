# -*- coding: utf-8 -*-
"""C 方案：分段结束立即转 MP4；空间不足停录并删除无法转换文件。"""
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "segment_remux"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
from src.recorder import RoomRecorder
import src.recorder as recmod
from src.manager import MonitorManager


def make_rec(base_name, segment=60, segment_remux_now=True, keep_source=False):
    return RoomRecorder(
        ffmpeg_path=str(ROOT / "bin" / "ffmpeg.exe"),
        stream_url="http://127.0.0.1:1/x.flv",
        output_dir=TMP,
        filename_base=base_name,
        name=base_name,
        remux_mp4=True,
        segment_remux_now=segment_remux_now,
        keep_source=keep_source,
        segment_duration=segment,
        min_free_gb=0,
    )


def fake_remux(ff, src):
    dst = Path(src).with_suffix(".mp4")
    dst.write_bytes(b"mp4" * 100)
    return True, str(dst)


def test_mode_flags():
    assert make_rec("m1", 60). _seg_mode is True
    assert make_rec("m2", 0)._seg_mode is False
    assert make_rec("m3", 60, segment_remux_now=False)._seg_mode is False
    print("PASS 分段模式开关")


def test_segment_convert_and_finalize():
    rec = make_rec("conv", 60)
    f0 = TMP / "conv_000.flv"; f0.write_bytes(b"x" * (1024 * 1024))
    f1 = TMP / "conv_001.flv"; f1.write_bytes(b"x" * (1024 * 1024))
    rec._refresh_files = lambda: None
    rec._files = [f0, f1]
    rec._current_file = str(f1)
    orig_remux = recmod.ffmpeg_util.remux_to_mp4
    orig_free = recmod.diskmod.free_gb
    recmod.ffmpeg_util.remux_to_mp4 = fake_remux
    recmod.diskmod.free_gb = lambda *_a, **_k: 100.0
    try:
        rec._enqueue_finished_segments()   # 只应转 000（001 正在写）
        with rec._seg_lock:
            queued = list(rec._seg_queue)
            rec._seg_queue.clear()
        print("queued:", [p.name for p in queued])
        assert queued == [f0], queued
        rec._convert_segment(f0)
        assert (TMP / "conv_000.mp4").exists(), "已完成分段没有转 MP4"
        assert not f0.exists(), "转成功应删除源文件"
        assert f1.exists(), "正在写的分段不应被处理"
        rec._current_file = ""
        out = rec._finalize_segments()
        print("finalize out:", [p.name for p in out])
        assert (TMP / "conv_001.mp4").exists()
        assert len(out) == 2, out
        assert not f1.exists()
    finally:
        recmod.ffmpeg_util.remux_to_mp4 = orig_remux
        recmod.diskmod.free_gb = orig_free
    print("PASS 分段完成即转、收尾转最后一段")


def test_space_fatal_delete():
    rec = make_rec("fatal", 60)
    src = TMP / "fatal_000.flv"
    src.write_bytes(b"x" * (2 * 1024 * 1024))
    rec._refresh_files = lambda: None
    rec._files = [src]
    rec._current_file = ""
    hits = []
    rec.on_critical = lambda reason: hits.append(reason)
    orig_free = recmod.diskmod.free_gb
    recmod.diskmod.free_gb = lambda *_a, **_k: 0.0005   # 0.5MB，不够
    try:
        rec._convert_segment(src)
    finally:
        recmod.diskmod.free_gb = orig_free
    print("fatal:", rec._seg_fatal, rec._seg_fatal_reason, "src exists:", src.exists())
    assert rec._seg_fatal is True
    assert not src.exists(), "空间不足无法转换的源文件应删除"
    assert rec._stop_event.is_set(), "空间不足应立即停止录制"
    deadline = time.time() + 5
    while time.time() < deadline and not hits:
        time.sleep(0.05)
    assert hits, "应通知 manager 停录"
    out = rec._finalize_segments()
    assert out == [], out
    print("PASS 空间不足：删除无法转换文件 + 立即停录 + 通知 manager")


def test_finalize_no_segment_space_delete():
    rec = make_rec("nonseg", 0)
    src = TMP / "nonseg.flv"
    src.write_bytes(b"x" * (1024 * 1024))
    rec._files = [src]
    rec._refresh_files = lambda: None
    orig_free = recmod.diskmod.free_gb
    recmod.diskmod.free_gb = lambda *_a, **_k: 0.001
    try:
        out = rec._finalize()
    finally:
        recmod.diskmod.free_gb = orig_free
    print("non-seg finalize:", out, "exists:", src.exists())
    assert out == []
    assert not src.exists(), "空间不足时不应保留无法转换的源文件"
    print("PASS 非分段模式空间不足也删除源文件")


def test_manager_critical():
    cfg = Config.load(TMP / "cfg.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["min_free_gb"] = 0
    cfg.save()
    wl = Watchlist(TMP / "wl.json"); wl.clear()
    mgr = MonitorManager(cfg, wl)
    assert mgr._disk_blocked is False
    mgr._on_recorder_critical("id", "测试磁盘不足")
    print("blocked:", mgr._disk_blocked)
    assert mgr._disk_blocked is True
    assert "磁盘" in mgr._gate_block_reason(0, 0.0, 0.0)
    print("PASS manager 收到致命磁盘告警后暂停开录")


def test_live_segmented_recording():
    """真实 ffmpeg 分段 2 秒：录 8 秒，停止后应得到多个 mp4 且无 FLV 残留。"""
    from src import ffmpeg_util
    from src.mock_api import configure as mock_configure
    from src.checker import LiveChecker
    cfg = Config.load(TMP / "live_cfg.json")
    ff = ffmpeg_util.resolve_ffmpeg(cfg.get_str("ffmpeg_path"))[0]
    mock_configure(ffmpeg_path=ff, live_seconds=60)
    checker = LiveChecker(cfg, use_mock=True)
    st = checker.check("111111111111")
    url = checker.choose_url(st)
    assert url, "mock 流地址为空"
    out = TMP / "live_out"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rec = RoomRecorder(
        ff, url, out, "seg_live",
        name="分段直播测试", remux_mp4=True, segment_duration=2,
        segment_remux_now=True, keep_source=False, min_free_gb=0,
        retry_max=1, retry_interval=1,
    )
    rec.start()
    time.sleep(8)
    rec.stop("测试结束")
    ok = rec.join(120)
    print("join:", ok, "files:", [p.name for p in out.iterdir()])
    files = list(out.iterdir())
    mp4s = [p for p in files if p.suffix.lower() == ".mp4"]
    flvs = [p for p in files if p.suffix.lower() in (".flv", ".ts")]
    assert len(mp4s) >= 2, "分段录制应产生多个 mp4: %s" % [p.name for p in files]
    assert not flvs, "分段转成功后不应残留 FLV/TS: %s" % [p.name for p in flvs]
    for p in mp4s:
        assert p.stat().st_size > 0
    print("PASS 真实分段录制：%d 个分段，全部即时转 MP4，无源文件残留" % len(mp4s))


def main():
    test_mode_flags()
    test_segment_convert_and_finalize()
    test_space_fatal_delete()
    test_finalize_no_segment_space_delete()
    test_manager_critical()
    test_live_segmented_recording()
    print("ALL SEGMENT REMUX TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
