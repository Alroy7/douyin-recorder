# -*- coding: utf-8 -*-
"""磁盘策略/诊断包/remux 空间预检 回归测试。"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "disk_policy"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
from src.manager import MonitorManager


def test_disk_policy():
    cfg = Config.load(TMP / "cfg.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["min_free_gb"] = 0
    cfg.save()
    wl = Watchlist(TMP / "wl.json")
    wl.clear()
    mgr = MonitorManager(cfg, wl)
    r = mgr.apply_disk_policy()
    print("reserve=0:", r)
    assert r["blocked"] is False
    assert mgr._gate_block_reason(0, 0.0, 0.0) == "" or "磁盘" not in mgr._gate_block_reason(0, 0.0, 0.0)

    cfg["min_free_gb"] = 10 ** 9      # 远超剩余空间 -> 立即阻断
    cfg.save()
    r = mgr.apply_disk_policy("测试")
    print("reserve=huge:", r)
    assert r["blocked"] is True
    assert "磁盘" in str(r["message"])
    assert "磁盘" in mgr._gate_block_reason(0, 0.0, 0.0)
    # 恢复
    cfg["min_free_gb"] = 0
    cfg.save()
    r2 = mgr.apply_disk_policy("恢复")
    assert r2["blocked"] is False
    assert "磁盘" not in mgr._gate_block_reason(0, 0.0, 0.0)
    print("PASS 磁盘保留值立即生效/恢复")


def test_remux_preflight():
    from src.recorder import RoomRecorder
    import src.recorder as recmod
    src = TMP / "src.flv"
    src.write_bytes(b"x" * (1024 * 1024))   # 1MB 假文件
    rec = RoomRecorder(
        ffmpeg_path=str(ROOT / "bin" / "ffmpeg.exe"),
        stream_url="http://127.0.0.1:1/x.flv",
        output_dir=TMP,
        filename_base="preflight",
        name="预检测试",
        remux_mp4=True,
        min_keep_seconds=0,
    )
    rec._refresh_files = lambda: None
    rec._files = [src]
    called = []
    rec._remux_files_limited = lambda files: called.append(list(files)) or list(files)

    orig_free = recmod.diskmod.free_gb
    recmod.diskmod.free_gb = lambda *_a, **_k: 0.01   # 10MB，不够 1MB+200MB
    try:
        out = rec._finalize()
    finally:
        recmod.diskmod.free_gb = orig_free
    print("preflight out:", out, "remux called:", len(called))
    assert not called, "空间不足时不应尝试 remux"
    assert out == [], "空间不足应返回空（文件已删除）"
    assert not src.exists(), "空间不足无法转换时删除源文件（v2.5.4 行为）"
    print("PASS 空间不足时跳过转 MP4 并删除源文件")


def test_diagnostics_export():
    cfg = Config.load(TMP / "diag_cfg.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["log_dir"] = str(TMP / "logs")
    cfg.save()
    Path(cfg["log_dir"]).mkdir(parents=True, exist_ok=True)
    (Path(cfg["log_dir"]) / "run-2026-11-15.log").write_text(
        "2026-11-15 10:00:00 [INFO] test\n", encoding="utf-8")
    wl = Watchlist(TMP / "diag_wl.json")
    wl.clear()
    wl.add_from_text("111111111111", name="诊断测试")
    from src.cli import export_diagnostics
    out = export_diagnostics(TMP / "diag.zip", cfg, wl, days=7)
    print("diag:", out, out.stat().st_size if out.exists() else -1)
    assert out.exists() and out.stat().st_size > 500
    import zipfile
    names = zipfile.ZipFile(out).namelist()
    assert "config.json" in names
    print("PASS 诊断包进程内导出成功")


def main():
    test_disk_policy()
    test_remux_preflight()
    test_diagnostics_export()
    print("ALL DISK POLICY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
