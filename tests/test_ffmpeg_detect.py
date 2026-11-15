# -*- coding: utf-8 -*-
"""ffmpeg 自动检测测试（任务 B）。

覆盖：
  1. 候选链顺序：config > 项目 bin > 原软件目录 > PATH > 常见目录 > 注册表；
  2. resolve_ffmpeg 优先用配置路径，配置失效后逐级回退（临时目录 + 假可执行文件）；
  3. 常见目录（LOCALAPPDATA 等环境变量）能被识别；
  4. validate=True 时会跳过无法运行的假 ffmpeg；
  5. 全部找不到时返回中文下载提示（含下载地址 / 放置位置 / 设置项）；
  6. open_download_page 打开 gyan.dev 下载页（用假 webbrowser 验证，不真的弹浏览器）；
  7. 本机真实 ffmpeg 能被解析并通过 -version 校验。

运行：python tests/test_ffmpeg_detect.py
"""
import contextlib
import logging
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src import ffmpeg_util

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("test.ffmpeg_detect")

TMP = ROOT / "tests" / "tmp" / "ffdetect"
ENV_KEYS = ("LOCALAPPDATA", "PROGRAMFILES", "ProgramFiles(x86)", "USERPROFILE", "ProgramData")


@contextlib.contextmanager
def isolated(which="", common_dirs=None, registry=None, local_bin=None):
    """隔离 ffmpeg 探测环境：替换 PATH/常见目录/注册表/项目 bin 候选，退出时恢复。"""
    saved = {
        "LOCAL_FFMPEG_CANDIDATES": ffmpeg_util.LOCAL_FFMPEG_CANDIDATES,
        "LEGACY_BUNDLED_FFMPEG": ffmpeg_util.LEGACY_BUNDLED_FFMPEG,
        "which": ffmpeg_util.shutil.which,
        "common": ffmpeg_util.common_ffmpeg_dirs,
        "registry": ffmpeg_util._registry_ffmpeg_paths,
    }
    saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        ffmpeg_util.LOCAL_FFMPEG_CANDIDATES = list(local_bin or [])
        ffmpeg_util.LEGACY_BUNDLED_FFMPEG = Path("Z:/nonexistent/cmdffmpeg.exe")
        ffmpeg_util.shutil.which = lambda *args, **kwargs: which
        ffmpeg_util.common_ffmpeg_dirs = lambda: list(common_dirs or [])
        ffmpeg_util._registry_ffmpeg_paths = lambda: list(registry or [])
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        ffmpeg_util.LOCAL_FFMPEG_CANDIDATES = saved["LOCAL_FFMPEG_CANDIDATES"]
        ffmpeg_util.LEGACY_BUNDLED_FFMPEG = saved["LEGACY_BUNDLED_FFMPEG"]
        ffmpeg_util.shutil.which = saved["which"]
        ffmpeg_util.common_ffmpeg_dirs = saved["common"]
        ffmpeg_util._registry_ffmpeg_paths = saved["registry"]
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def reset_tmp():
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)


def make_fake(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake ffmpeg for test")
    return path


def test_candidate_order():
    reset_tmp()
    configured = TMP / "custom" / "ffmpeg.exe"
    project_exe = TMP / "proj" / "bin" / "ffmpeg.exe"
    project_noext = TMP / "proj" / "bin" / "ffmpeg"
    path_exe = TMP / "path" / "ffmpeg.exe"
    legacy = Path(r"D:\抖音录制\抖音直播录制工具\PotPlayer\cmdffmpeg.exe")
    common = TMP / "common" / "ffmpeg" / "bin"
    registry = TMP / "registry" / "ffmpeg.exe"
    with isolated(which=str(path_exe), common_dirs=[common], registry=[registry],
                  local_bin=[project_exe, project_noext]):
        ffmpeg_util.LEGACY_BUNDLED_FFMPEG = legacy
        candidates = ffmpeg_util.iter_ffmpeg_candidates(str(configured))
        names = [str(p).lower() for p in candidates]
        def index(target):
            return names.index(str(Path(target)).lower())
        order = [
            index(configured),
            index(project_exe),
            index(project_noext),
            index(legacy),
            index(path_exe),
            index(common / "ffmpeg.exe"),
            index(registry),
        ]
        LOG.info("候选链: %s", [str(p) for p in candidates])
        assert order == sorted(order), "候选链顺序错误: %s" % order
        assert len(order) == len(set(order))
        # 去重：同一个路径不会重复出现
        assert len(names) == len(set(names)), "候选链存在重复项"
    return True


def test_resolve_prefers_configured_and_falls_back():
    reset_tmp()
    configured = make_fake(TMP / "configured" / "ffmpeg.exe")
    bin_exe = make_fake(TMP / "bin" / "ffmpeg.exe")
    path_exe = make_fake(TMP / "path" / "ffmpeg.exe")
    with isolated(which=str(path_exe), local_bin=[bin_exe]):
        found, err = ffmpeg_util.resolve_ffmpeg(str(configured), validate=False)
        assert found == str(configured.resolve()), (found, err)
        configured.unlink()
        found2, err2 = ffmpeg_util.resolve_ffmpeg(str(configured), validate=False)
        assert found2 == str(bin_exe.resolve()), (found2, err2)
        bin_exe.unlink()
        found3, err3 = ffmpeg_util.resolve_ffmpeg(str(configured), validate=False)
        assert found3 == str(path_exe.resolve()), (found3, err3)
    return True


def test_common_env_dirs():
    reset_tmp()
    appdata = TMP / "appdata"
    exe = make_fake(appdata / "ffmpeg" / "bin" / "ffmpeg.exe")
    saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["LOCALAPPDATA"] = str(appdata)
        os.environ["PROGRAMFILES"] = str(TMP / "no_such_pf")
        os.environ["ProgramFiles(x86)"] = str(TMP / "no_such_pf86")
        os.environ["USERPROFILE"] = str(TMP / "user")
        os.environ["ProgramData"] = str(TMP / "programdata")
        dirs = [str(p).lower() for p in ffmpeg_util.common_ffmpeg_dirs()]
        expect = str(appdata / "ffmpeg" / "bin").lower()
        assert expect in dirs, "LOCALAPPDATA 常见目录未识别: %s" % dirs
        # resolve 也能从中找到（隔离掉其它来源）
        with isolated(common_dirs=[appdata / "ffmpeg" / "bin"]):
            found, err = ffmpeg_util.resolve_ffmpeg(validate=False)
            assert found == str(exe.resolve()), (found, err)
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return True


def test_validate_skips_broken():
    reset_tmp()
    broken = make_fake(TMP / "broken" / "ffmpeg.exe")
    with isolated(local_bin=[broken]):
        found, err = ffmpeg_util.resolve_ffmpeg(validate=True)
        LOG.info("坏候选校验: found=%r err=%s", found, str(err).splitlines()[0] if err else "")
        assert found == "", "无法运行的假 ffmpeg 不应被接受: %r" % found
        assert err
    return True


def test_missing_hint():
    with isolated():
        found, err = ffmpeg_util.resolve_ffmpeg()
        assert found == ""
        assert "gyan.dev" in err and "ffmpeg.org" in err
        assert "bin" in err and "ffmpeg_path" in err
        hint = ffmpeg_util.missing_ffmpeg_hint()
        for token in ("gyan.dev/ffmpeg/builds", "ffmpeg.org/download", "bin", "ffmpeg_path",
                      "run.py --check", "不会自动下载"):
            assert token in hint, "缺失提示缺少关键信息: %s" % token
        LOG.info("缺失提示（前 2 行）: %s", " / ".join(hint.splitlines()[:2]))
    return True


def test_open_download_page():
    opened = []
    saved = ffmpeg_util.webbrowser
    try:
        class _FakeWB:
            @staticmethod
            def open(url):
                opened.append(url)
                return True
        ffmpeg_util.webbrowser = _FakeWB
        assert ffmpeg_util.open_download_page() is True
        assert opened and "gyan.dev" in opened[0], opened
    finally:
        ffmpeg_util.webbrowser = saved
    return True


def test_real_ffmpeg():
    path, err = ffmpeg_util.resolve_ffmpeg()
    assert path, "本机应能解析到 ffmpeg（项目 bin/ffmpeg.exe）: %s" % err
    ok, info = ffmpeg_util.probe_ffmpeg(path)
    LOG.info("真实 ffmpeg: %s -> %s", path, info)
    assert ok, info
    return True


TESTS = [
    ("候选链顺序", test_candidate_order),
    ("配置优先与回退", test_resolve_prefers_configured_and_falls_back),
    ("常见目录识别", test_common_env_dirs),
    ("坏候选校验", test_validate_skips_broken),
    ("缺失提示", test_missing_hint),
    ("下载页", test_open_download_page),
    ("真实 ffmpeg", test_real_ffmpeg),
]


def main():
    failed = []
    for name, fn in TESTS:
        LOG.info("========== 开始: %s ==========", name)
        try:
            fn()
            LOG.info("========== 通过: %s ==========", name)
        except Exception as exc:
            LOG.exception("========== 失败: %s -> %s ==========", name, exc)
            failed.append((name, str(exc)))
    print()
    if failed:
        print("FAILED:")
        for name, exc in failed:
            print("  -", name, ":", exc)
        return 1
    print("ALL FFMPEG DETECT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
