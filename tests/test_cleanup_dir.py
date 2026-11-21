# -*- coding: utf-8 -*-
"""输出目录清理测试（空文件夹 / 失败 flv / 不完整 mp4 / 过短 mp4）。

运行：python tests/test_cleanup_dir.py
"""
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

from src import cleanup_dir
from src import ffmpeg_util

TMP = ROOT / "tests" / "tmp" / "cleanup_dir"


def _make_tree() -> Path:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / "a").mkdir()
    (TMP / "b" / "c").mkdir(parents=True)

    (TMP / "leftover_000.flv").write_bytes(b"flv" * 100)
    (TMP / "leftover_000.mp4").write_bytes(b"mp4" * 100)   # 有效 mp4 -> flv 是重复残留

    (TMP / "convert_000.flv").write_bytes(b"flv" * 100)    # 无 mp4 -> 补转成功

    (TMP / "failed_000.flv").write_bytes(b"flv" * 100)     # 无 mp4，且补转失败 -> 删除

    (TMP / "incomplete.mp4").write_bytes(b"")              # 0 字节
    (TMP / "broken.mp4").write_bytes(b"mp4" * 100)         # 解析不出时长
    (TMP / "short.mp4").write_bytes(b"mp4" * 100)          # 100s < 900s
    (TMP / "good.mp4").write_bytes(b"mp4" * 100)           # 3000s 保留

    old = time.time() - 3600
    for p in TMP.rglob("*"):
        if p.is_file():
            import os
            os.utime(p, (old, old))
    return TMP


def _patch():
    orig_probe = ffmpeg_util.probe_duration
    orig_remux = ffmpeg_util.remux_to_mp4

    def fake_probe(_ff, path, **_kw):
        name = Path(path).name
        if name in ("leftover_000.mp4", "good.mp4"):
            return 3000.0
        if name == "short.mp4":
            return 100.0
        return -1.0

    def fake_remux(_ff, src, **_kw):
        src = Path(src)
        if src.name == "convert_000.flv":
            dst = src.with_suffix(".mp4")
            dst.write_bytes(b"mp4" * 100)
            return True, str(dst)
        return False, "mock fail"

    ffmpeg_util.probe_duration = fake_probe
    ffmpeg_util.remux_to_mp4 = fake_remux
    return orig_probe, orig_remux


def _restore(orig):
    ffmpeg_util.probe_duration, ffmpeg_util.remux_to_mp4 = orig


def test_dry_run_no_delete():
    root = _make_tree()
    orig = _patch()
    try:
        stats = cleanup_dir.cleanup_directory(root, "ffmpeg", 900,
                                              min_age_seconds=0, dry_run=True)
    finally:
        _restore(orig)
    assert stats["dry_run"] is True, stats
    assert stats["plan_flv_convert"] == 2, stats     # convert + failed（都无 mp4）
    assert stats["plan_flv_leftover"] == 1, stats    # leftover
    assert stats["plan_mp4_incomplete"] == 2, stats  # incomplete + broken
    assert stats["plan_mp4_short"] == 1, stats       # short
    assert (root / "leftover_000.flv").exists(), "dry_run 不应删除"
    assert (root / "a").is_dir()
    print("PASS 扫描预览（不删除）")


def test_execute_cleanup():
    root = _make_tree()
    orig = _patch()
    try:
        stats = cleanup_dir.cleanup_directory(root, "ffmpeg", 900,
                                              min_age_seconds=0, dry_run=False)
    finally:
        _restore(orig)
    print("stats:", {k: v for k, v in stats.items() if k != "errors"})
    assert stats["flv_converted"] == 1, stats
    assert stats["flv_deleted"] == 1, stats
    assert stats["flv_leftover_deleted"] == 1, stats
    assert stats["mp4_incomplete_deleted"] == 2, stats
    assert stats["mp4_short_deleted"] == 1, stats
    assert stats["empty_dirs_deleted"] == 3, stats   # a, b/c, b

    assert not (root / "leftover_000.flv").exists()
    assert (root / "leftover_000.mp4").exists(), "有效 mp4 应保留"
    assert not (root / "convert_000.flv").exists()
    assert (root / "convert_000.mp4").exists(), "补转成功应生成 mp4"
    assert not (root / "failed_000.flv").exists(), "转换失败的 flv 应删除"
    assert not (root / "incomplete.mp4").exists()
    assert not (root / "broken.mp4").exists()
    assert not (root / "short.mp4").exists()
    assert (root / "good.mp4").exists(), "正常 mp4 应保留"
    assert not (root / "a").exists(), "空文件夹应删除"
    assert not (root / "b").exists(), "空文件夹应删除"
    assert root.exists(), "根目录不能删"
    print("PASS 执行清理：空目录/flv/不完整/过短全部处理，正常文件保留")


def test_protect_skips_active_files():
    root = _make_tree()
    protect = [root / "short.mp4", root / "failed_000.flv"]
    orig = _patch()
    try:
        stats = cleanup_dir.cleanup_directory(root, "ffmpeg", 900,
                                              min_age_seconds=0,
                                              protect=protect, dry_run=False)
    finally:
        _restore(orig)
    assert (root / "short.mp4").exists(), "受保护文件不应删除"
    assert (root / "failed_000.flv").exists(), "受保护文件不应删除"
    assert stats.get("mp4_short_deleted") == 0, stats
    print("PASS 受保护文件不会被清理")


def test_min_age_skips_new_files():
    root = _make_tree()
    # 把所有文件的修改时间改成「刚刚」，min_age=120 应该全部跳过
    import os
    now = time.time()
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (now, now))
    orig = _patch()
    try:
        stats = cleanup_dir.cleanup_directory(root, "ffmpeg", 900,
                                              min_age_seconds=120, dry_run=False)
    finally:
        _restore(orig)
    assert stats["mp4_short_deleted"] == 0, stats
    assert stats["flv_deleted"] == 0, stats
    assert (root / "short.mp4").exists()
    print("PASS 太新的文件（可能正在写）不清理")


def main():
    tests = [
        ("扫描预览", test_dry_run_no_delete),
        ("执行清理", test_execute_cleanup),
        ("保护录制中文件", test_protect_skips_active_files),
        ("太新文件跳过", test_min_age_skips_new_files),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failed.append((name, str(exc)))
    print()
    if failed:
        print("FAILED:")
        for n, e in failed:
            print("  -", n, ":", e)
        return 1
    print("ALL CLEANUP_DIR TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
