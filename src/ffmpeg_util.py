# -*- coding: utf-8 -*-
"""ffmpeg 相关工具：探测可执行文件、版本自检、解析媒体时长、remux 转 MP4、子进程树清理。

设计要点：
- ffmpeg 不在 PATH 里也能用：按「配置 > 项目 bin > 原软件目录 > PATH > 常见安装目录 > 注册表」顺序查找；
- 找到候选后执行 -version 校验，坏候选自动跳过，避免配置写错后直接崩溃；
- 找不到时给中文提示 + 下载地址（只提示，不自动下载/安装）；
- subprocess 一律传 list 参数（中文路径安全），并加 CREATE_NO_WINDOW 避免 GUI 弹黑框。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

LOG = logging.getLogger("dyrec.ffmpeg")

#: Windows 下隐藏控制台窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: 项目自带的 ffmpeg（随工具分发，卸载原软件也能用）
LOCAL_FFMPEG_CANDIDATES = [
    PROJECT_ROOT / "bin" / "ffmpeg.exe",
    PROJECT_ROOT / "bin" / "ffmpeg",
]
#: 原版软件目录里的 ffmpeg（兼容旧用户/未安装自带版本时）
LEGACY_BUNDLED_FFMPEG = Path(r"D:\抖音录制\抖音直播录制工具\PotPlayer\cmdffmpeg.exe")

#: 缺失 ffmpeg 时推荐的下载页（按推荐程度排序）
DOWNLOAD_PAGE = "https://www.gyan.dev/ffmpeg/builds/"
DOWNLOAD_PAGES: List[Tuple[str, str]] = [
    ("gyan.dev（推荐，Windows 版）", "https://www.gyan.dev/ffmpeg/builds/"),
    ("FFmpeg 官网下载页", "https://ffmpeg.org/download.html"),
    ("BtbN 构建（GitHub Releases）", "https://github.com/BtbN/FFmpeg-Builds/releases"),
]

#: 浏览器 UA（抖音拉流带上更稳，参考原版程序录制命令）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

#: HTTP(S) 直播流默认输入参数（来自原版程序内存中的 ffmpeg 命令模板 + 有界重连）
#: - reconnect 系列让 ffmpeg 内部做短重连（双保险）；
#: - reconnect_max_retries / reconnect_delay_total_max 限制总重试，避免 URL 失效时无限卡住，
#:   超限后 ffmpeg 退出，由上层「重新取流地址 + 重启 ffmpeg」接管。
DEFAULT_HTTP_INPUT_ARGS = [
    "-user_agent", DEFAULT_USER_AGENT,
    "-referer", "https://live.douyin.com/",
    "-timeout", "15000000",
    "-rw_timeout", "15000000",
    "-reconnect", "1",
    "-reconnect_on_network_error", "1",
    "-reconnect_on_http_error", "403,404,500,502,503,504",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "10",
    "-reconnect_max_retries", "3",
    "-reconnect_delay_total_max", "30",
    "-fflags", "+genpts",
    "-analyzeduration", "5000000",
    "-probesize", "5000000",
]

#: 解析 `ffmpeg -i` 输出的 Duration 行
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


class FFmpegNotFound(RuntimeError):
    """找不到可用的 ffmpeg。"""


def _is_executable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _env_dir(name: str) -> Optional[Path]:
    """读取环境变量里的目录路径（空时返回 None）。"""
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def common_ffmpeg_dirs() -> List[Path]:
    """常见 ffmpeg 安装目录（按优先级；目录里是否真有文件由候选过滤负责）。

    覆盖：C:\\ffmpeg、LOCALAPPDATA、Program Files、scoop、chocolatey、winget Links。
    """
    dirs: List[Path] = [Path(r"C:\ffmpeg\bin"), Path(r"C:\ffmpeg")]
    pairs = (
        ("LOCALAPPDATA", r"ffmpeg\bin"),
        ("PROGRAMFILES", r"ffmpeg\bin"),
        ("ProgramFiles(x86)", r"ffmpeg\bin"),
        ("USERPROFILE", r"scoop\shims"),
        ("ProgramData", r"chocolatey\bin"),
        ("LOCALAPPDATA", r"Microsoft\WinGet\Links"),
    )
    for env_name, sub in pairs:
        base = _env_dir(env_name)
        if base is not None:
            dirs.append(base / sub)
    return dirs


def _registry_ffmpeg_paths() -> List[Path]:
    """注册表 App Paths\\ffmpeg.exe（HKLM/HKCU，64/32 位视图）中登记的 ffmpeg 路径。"""
    if os.name != "nt":
        return []
    try:
        import winreg  # type: ignore
    except ImportError:
        return []
    results: List[Path] = []
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\ffmpeg.exe"
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    views = (
        winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        winreg.KEY_READ | getattr(winreg, "KEY_WOW64_32KEY", 0),
    )
    for hive in hives:
        found = False
        for view in views:
            if found:
                break
            try:
                with winreg.OpenKey(hive, subkey, 0, view) as key:
                    value, _kind = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            text = str(value or "").strip().strip('"')
            if text:
                results.append(Path(text))
                found = True
    return results


def iter_ffmpeg_candidates(configured: str = "", search_dir: Optional[Path] = None,
                           target_default: str = "") -> List[Path]:
    """按优先级生成 ffmpeg 候选路径列表（已去重，保持顺序）。

    顺序（前者优先）：
      1. config.json 的 ffmpeg_path（非空；相对路径还会尝试 cwd / 项目根）
      2. <项目根>/bin/ffmpeg.exe、<项目根>/bin/ffmpeg（随工具自带，推荐）
      3. 原软件目录 PotPlayer\\cmdffmpeg.exe（兼容旧用户）
      4. 调用方额外提示的 target_default / search_dir（兼容旧接口）
      5. 系统 PATH（shutil.which）
      6. 常见安装目录（C:\\ffmpeg\\bin、LOCALAPPDATA、Program Files、scoop、chocolatey、winget）
      7. 注册表 App Paths（HKLM/HKCU）

    这是「优先级顺序」的唯一来源，resolve_ffmpeg 与测试都依赖它。
    """
    candidates: List[Path] = []

    def add(path) -> None:
        if path is None or str(path).strip() == "":
            return
        candidates.append(Path(path))

    if configured and str(configured).strip():
        configured_path = Path(str(configured).strip())
        add(configured_path)
        if not configured_path.is_absolute():  # 也支持只写可执行文件名
            add(Path.cwd() / configured_path)
            add(PROJECT_ROOT / configured_path)

    for local in LOCAL_FFMPEG_CANDIDATES:
        add(local)
    add(LEGACY_BUNDLED_FFMPEG)

    if target_default:
        add(target_default)
    if search_dir:
        add(Path(search_dir) / "cmdffmpeg.exe")
        add(Path(search_dir) / "ffmpeg.exe")
        add(Path(search_dir) / "ffmpeg")

    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        add(found)

    for directory in common_ffmpeg_dirs():
        add(directory / "ffmpeg.exe")
        add(directory / "ffmpeg")
    for path in _registry_ffmpeg_paths():
        add(path)

    # 去重（Windows 大小写不敏感），保留第一次出现的位置
    unique: List[Path] = []
    seen = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def resolve_ffmpeg(configured: str = "", search_dir: Optional[Path] = None,
                   target_default: str = "", validate: bool = True) -> Tuple[str, str]:
    """返回 (ffmpeg 路径, 错误信息)。找不到时路径为空字符串。

    解析顺序见 iter_ffmpeg_candidates()。validate=True（默认）时对找到的候选执行
    `ffmpeg -version` 校验，坏候选会跳过并继续查找下一个。
    """
    for candidate in iter_ffmpeg_candidates(configured, search_dir, target_default):
        if not _is_executable(candidate):
            continue
        if validate:
            ok, info = probe_ffmpeg(str(candidate))
            if not ok:
                LOG.warning("候选 ffmpeg 无法运行，继续查找下一个: %s（%s）", candidate, info)
                continue
        return str(candidate.resolve()), ""
    msg = missing_ffmpeg_hint()
    if configured:
        msg += "\n（配置的 ffmpeg_path 不可用：%s）" % configured
    return "", msg


def probe_ffmpeg(ffmpeg_path: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """运行 ffmpeg -version 校验可用性，返回 (ok, 版本信息/错误)。"""
    if not ffmpeg_path:
        return False, "ffmpeg 路径为空"
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-version"],
            capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"无法启动 ffmpeg: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return False, f"ffmpeg 返回码 {proc.returncode}: {err[:300]}"
    first = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
    version = first[0] if first else "unknown"
    return True, version


def check_ffmpeg(configured: str = "", search_dir: Optional[Path] = None,
                 target_default: str = "") -> Tuple[bool, str, str]:
    """完整自检：返回 (ok, ffmpeg_path, 说明)。"""
    path, err = resolve_ffmpeg(configured, search_dir, target_default)
    if not path:
        return False, "", err
    ok, info = probe_ffmpeg(path)
    if not ok:
        return False, path, f"{path} 无法运行：{info}"
    return True, path, info


def missing_ffmpeg_hint() -> str:
    """未找到 ffmpeg 时给用户的中文提示：下载地址 + 放置位置 + 设置项。"""
    lines = [
        "未找到可用的 ffmpeg！ffmpeg 是录制必需组件（本工具不捆绑、也不会自动下载安装）。",
        "",
        "解决办法（任选其一）：",
        "  1) 下载 Windows 版 ffmpeg（免费开源）：",
    ]
    for title, url in DOWNLOAD_PAGES:
        lines.append("       - %s：%s" % (title, url))
    lines.extend([
        "  2) 解压后把压缩包里的 bin\\ffmpeg.exe 复制到项目 bin 目录：",
        "       %s" % (PROJECT_ROOT / "bin" / "ffmpeg.exe"),
        "  3) 或在「设置 → ffmpeg 路径」里指定 ffmpeg.exe 的绝对路径（保存到 config.json 的 ffmpeg_path）",
        "  4) 或把 ffmpeg 所在目录加入系统 PATH，然后重新打开本程序",
        "",
        "放置/设置完成后运行 `python run.py --check` 验证。",
    ])
    return "\n".join(lines)


def open_download_page(url: str = DOWNLOAD_PAGE) -> bool:
    """用系统默认浏览器打开 ffmpeg 下载页（只提示下载，不自动安装）。"""
    try:
        return bool(webbrowser.open(str(url or DOWNLOAD_PAGE)))
    except Exception as exc:  # pragma: no cover - 无浏览器/被策略拦截
        LOG.warning("打开下载页失败: %s", exc)
        return False


def probe_duration(ffmpeg: str, media: Path, timeout: float = 60.0) -> float:
    """解析媒体文件时长（秒）；失败返回 -1。

    只运行 ffmpeg -i <file> 读取 stderr 里的 Duration 行，不做转码，速度快。
    """
    media = Path(media)
    if not ffmpeg or not media.is_file():
        return -1.0
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(media)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return -1.0
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    match = _DURATION_RE.search(stderr)
    if not match:
        return -1.0
    hours, minutes, seconds = match.groups()
    try:
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:  # pragma: no cover - 正则已保证格式
        return -1.0


def probe_total_duration(ffmpeg: str, files: Iterable[Path]) -> float:
    """累加多个录制文件（分段/重连）的时长；任一文件解析失败返回 -1。

    调用方在返回 -1 时应回落到运行时计时（started_at/ended_at 差值）。
    """
    total = 0.0
    count = 0
    for item in files:
        duration = probe_duration(ffmpeg, item)
        if duration < 0:
            return -1.0
        total += duration
        count += 1
    return total if count else -1.0


def build_ffmpeg_cmd(ffmpeg: str, input_url: str, output_target: str,
                     segment_duration: int = 0,
                     container: str = "flv",
                     extra_args: Optional[List[str]] = None,
                     extra_input_args: Optional[List[str]] = None,
                     http_input_args: Optional[List[str]] = None) -> List[str]:
    """构造录制命令（list 形式，能正确处理中文/空格路径）。

    - 不分段：直接单文件输出；
    - 分段：segment muxer + %03d 序号，避免重连时覆盖旧片段；
    - http(s) 流自动加上浏览器 UA / Referer / reconnect / genpts 等参数；
    - 本地文件/测试源不加网络参数（避免 ffmpeg 报未知选项）。
    """
    cmd: List[str] = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
    ]
    is_network = str(input_url).lower().startswith(("http://", "https://", "rtmp://", "rtmps://"))
    if is_network:
        net_args = DEFAULT_HTTP_INPUT_ARGS if http_input_args is None else list(http_input_args)
        cmd.extend(str(a) for a in net_args)
    if extra_input_args:
        cmd.extend(str(a) for a in extra_input_args)
    cmd += ["-i", input_url, "-c", "copy"]
    if segment_duration and segment_duration > 0:
        # 分段输出：output_target 需包含 %03d 之类序号占位
        cmd += [
            "-f", "segment",
            "-segment_time", str(int(segment_duration)),
            "-reset_timestamps", "1",
            "-segment_format", "flv" if container == "flv" else "mpegts",
            "-segment_start_number", "0",
        ]
    elif container == "ts":
        cmd += ["-f", "mpegts"]
    else:
        cmd += ["-f", "flv", "-flush_packets", "1"]
    if extra_args:
        cmd.extend(str(a) for a in extra_args)
    cmd.append(output_target)
    return cmd


def remux_to_mp4(ffmpeg: str, src: Path, dst: Optional[Path] = None,
                 timeout: float = 1800, extra_args: Optional[List[str]] = None) -> Tuple[bool, str]:
    """无损 remux：flv/ts -> mp4（-c copy）。成功后删除源文件？由调用方决定。"""
    src = Path(src)
    dst = Path(dst) if dst else src.with_suffix(".mp4")
    if not src.exists():
        return False, f"源文件不存在: {src}"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
           "-i", str(src), "-c", "copy", "-movflags", "+faststart"]
    if extra_args:
        cmd.extend(str(a) for a in extra_args)
    cmd.append(str(dst))
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"remux 失败: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        # 失败时删除半成品 MP4（空间不足会留下无法播放的残缺文件）
        try:
            if dst.exists():
                dst.unlink()
        except OSError:
            pass
        return False, f"remux 失败(rc={proc.returncode}): {err[-500:]}"
    return True, str(dst)


def _taskkill(pid: int, force: bool = True) -> None:
    """Windows 下用 taskkill 杀进程树（兜底，防止 ffmpeg 残留子进程）。"""
    if os.name != "nt":
        return
    cmd = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        cmd.insert(1, "/F")
    try:
        subprocess.run(cmd, capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass


def terminate_process(proc: Optional["subprocess.Popen"], wait: float = 5.0) -> None:
    """优雅终止 ffmpeg：先 terminate，等待；超时再 taskkill /F /T 杀进程树。"""
    if proc is None or proc.poll() is not None:
        return
    pid = proc.pid
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=wait)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    _taskkill(pid, force=True)


def kill_process_tree(pid: int) -> None:
    """外部进程树强杀（退出清理时兜底使用）。"""
    _taskkill(pid, force=True)


def read_tail(stream, max_lines: int = 60) -> str:
    """读取 stderr 尾部（供诊断）。需在独立线程调用，stream 为 bytes。"""
    lines = []
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            lines.append(line)
            if len(lines) > max_lines:
                lines.pop(0)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass
    return "\n".join(lines)
