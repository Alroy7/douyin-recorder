# -*- coding: utf-8 -*-
"""补转历史录像测试：扫描/转换/删源/keep_source/CLI。"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "remux_legacy"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src import remux_legacy, ffmpeg_util

FF = str(ROOT / "bin" / "ffmpeg.exe")
ok, FF, info = ffmpeg_util.check_ffmpeg(FF)
assert ok, info


def make_flv(path: Path, seconds: float = 2.0):
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=duration=%s:size=160x120:rate=10" % seconds,
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-f", "flv", str(path)]
    subprocess.run(cmd, check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_ts(path: Path, seconds: float = 2.0):
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=duration=%s:size=160x120:rate=10" % seconds,
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-f", "mpegts", str(path)]
    subprocess.run(cmd, check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_find_and_convert():
    d = TMP / "out"
    d.mkdir(parents=True, exist_ok=True)
    make_flv(d / "a.flv")
    make_ts(d / "b.ts")
    make_flv(d / "c.flv")
    (d / "c.mp4").write_bytes(b"existing")
    files = remux_legacy.find_sources(d, min_age_seconds=0)
    names = sorted(p.name for p in files)
    print("found:", names)
    assert names == ["a.flv", "b.ts"], names
    result = remux_legacy.convert_sources(files, FF, keep_source=False)
    print("result:", {k: v for k, v in result.items() if k != "errors"})
    assert result["converted"] == 2 and result["failed"] == 0, result
    assert (d / "a.mp4").exists() and (d / "b.mp4").exists()
    assert not (d / "a.flv").exists() and not (d / "b.ts").exists()
    # min_age 过滤
    make_flv(d / "new.flv")
    assert remux_legacy.find_sources(d, min_age_seconds=3600) == []
    print("PASS 扫描/转换/删除源文件")
    return d


def test_keep_source(d):
    make_flv(d / "keep.flv")
    files = [p for p in remux_legacy.find_sources(d, min_age_seconds=0)
             if p.name == "keep.flv"]
    assert [p.name for p in files] == ["keep.flv"], [p.name for p in files]
    result = remux_legacy.convert_sources(files, FF, keep_source=True)
    assert result["converted"] == 1 and (d / "keep.flv").exists() and (d / "keep.mp4").exists()
    print("PASS keep_source 保留原文件")


def test_cli():
    d = TMP / "cli"
    d.mkdir(parents=True, exist_ok=True)
    make_flv(d / "cli1.flv")
    # CLI 只补转 120 秒以前的文件，这里把修改时间往前调
    old = time.time() - 300
    os.utime(d / "cli1.flv", (old, old))
    cfg = TMP / "cfg_cli.json"
    cfg.write_text('{"output_dir": "%s", "keep_source": false, "ffmpeg_path": "%s"}'
                   % (str(d).replace("\\", "\\\\"), FF.replace("\\", "\\\\")), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, str(ROOT / "run.py"), "--config", str(cfg),
                        "--convert-leftovers"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=180)
    print("cli out:", r.stdout.strip()[:400])
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    assert "补转完成" in r.stdout
    assert (d / "cli1.mp4").exists() and not (d / "cli1.flv").exists()
    print("PASS CLI 补转")


def main():
    d = test_find_and_convert()
    test_keep_source(d)
    test_cli()
    print("ALL REMUX_LEGACY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
