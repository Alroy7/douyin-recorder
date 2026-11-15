# -*- coding: utf-8 -*-
"""命令行模式：无 GUI 常驻监控 / 只查一次状态 / 列表 / 添加主播。

用法（从项目根运行）：
    python run.py --cli          常驻监控（Ctrl+C 退出，会优雅停止录制）
    python run.py --once         只查询一次所有主播的直播状态，不录制
    python run.py --list         列出主播列表
    python run.py --add <链接/房间号> --name 主播名
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import List

from . import disk as diskmod
from .checker import LiveChecker
from .manager import MonitorManager
from .model import STATUS_ERROR, WatchItem
from .storage import Watchlist

LOG = logging.getLogger("dyrec.cli")


def _print_table(rows: List[dict]) -> None:
    """打印状态表（中文对齐用固定列宽近似处理）。"""
    if not rows:
        print("（主播列表为空，请先用 --add 添加）")
        return
    header = "{:<4} {:<18} {:<14} {:<8} {:<10} {:<9} {:<10}".format(
        "#", "主播", "房间号", "直播", "状态", "时长", "大小")
    print(header)
    print("-" * 88)
    for idx, row in enumerate(rows, 1):
        print("{:<4} {:<18} {:<14} {:<8} {:<10} {:<9} {:<10}".format(
            idx,
            _clip(row.get("name") or "", 16),
            _clip(row.get("room") or "", 12),
            "是" if row.get("is_live") else "否",
            _clip(row.get("status") or "", 8),
            diskmod.human_duration(row.get("duration_sec") or 0),
            diskmod.human_size(row.get("file_size") or 0),
        ))
        if row.get("error"):
            print("     错误: " + _clip(str(row["error"]), 100))


def _clip(text: str, width: int) -> str:
    text = str(text)
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def cmd_once(config, watchlist: Watchlist) -> int:
    """查询一次所有主播状态，不启动录制。"""
    checker = LiveChecker(config)
    print("协议层: %s" % checker.describe())
    items = watchlist.all()
    if not items:
        print("（主播列表为空，请先添加主播：python run.py --add <链接/房间号>）")
        return 0
    print("正在查询 %d 个主播的直播状态..." % len(items))
    live_count = 0
    for item in items:
        st = checker.check(item)
        if st.is_live:
            live_count += 1
        status = "直播中" if st.is_live else ("查询失败" if st.error else "未开播")
        print("  [%s] %s（房间号 %s）标题: %s" % (
            status, item.display_name, item.web_rid or "?", st.title or "-"))
        if st.error:
            print("      错误: %s" % st.error)
        if st.is_live:
            url = checker.choose_url(st, item.quality)
            print("      可录制流: %s" % ("有" if url else "无"))
    print("共 %d 个主播，%d 个正在直播" % (len(items), live_count))
    return 0


def cmd_list(watchlist: Watchlist) -> int:
    """列出主播列表。"""
    items = watchlist.all()
    if not items:
        print("（主播列表为空）")
        return 0
    print("共 %d 个主播：" % len(items))
    for idx, item in enumerate(items, 1):
        print("  %d. %s  房间号=%s  自动录制=%s  画质=%s  启用=%s  id=%s" % (
            idx, item.display_name, item.web_rid or "-", "是" if item.auto_record else "否",
            item.quality, "是" if item.enabled else "否", item.id))
    return 0


def cmd_add(config, watchlist: Watchlist, target: str, name: str = "") -> int:
    """添加主播（联网解析房间号/主播名，失败也能本地解析添加）。"""
    checker = LiveChecker(config)
    info = checker.resolve(target)
    item = WatchlistItemFromInfo(watchlist, target, name, info)
    # 若能查到直播状态，顺便补全主播名
    if not item.name and not info.get("error"):
        try:
            st = checker.check(item)
            if st.anchor_name:
                item.name = st.anchor_name
                watchlist.update(item)
        except Exception:  # pragma: no cover
            pass
    print("已添加主播: %s（房间号 %s）" % (item.display_name, item.web_rid or "?"))
    return 0


def WatchlistItemFromInfo(watchlist: Watchlist, target: str, name: str, info: dict) -> WatchItem:
    """用解析结果构造条目并写入 watchlist。"""
    room = str(info.get("web_rid") or "")
    anchor = name or str(info.get("name") or "")
    item = watchlist.add_from_text(target, name=anchor)
    if room and not item.web_rid:
        item.web_rid = room
    if info.get("url") and not item.url:
        item.url = str(info["url"])
    watchlist.update(item)
    return item


def cmd_monitor(config, watchlist: Watchlist) -> int:
    """常驻监控（Ctrl+C 退出）。"""
    manager = MonitorManager(config, watchlist)
    ok, msg = manager.start()
    if not ok:
        print("启动失败: " + msg, file=sys.stderr)
        return 1
    print("监控已启动（Ctrl+C 退出）。协议层: %s，检查间隔: %d 秒" % (
        manager.checker.describe(), config.get_int("check_interval")))
    interval = config.get_int("check_interval")
    last_print = 0.0
    try:
        while True:
            time.sleep(1)
            now = time.time()
            # 每 15 秒或检查间隔内打印一次状态表
            if now - last_print >= min(15, max(5, interval)):
                last_print = now
                print()
                print("===== %s =====" % time.strftime("%Y-%m-%d %H:%M:%S"))
                _print_table(manager.snapshot())
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止监控并等待 ffmpeg 退出...")
    finally:
        manager.stop(wait=True, timeout=40)
        print("监控已停止。")
    return 0


def cmd_history(config, watchlist, days: int = 0) -> int:
    """--history：打印最近录制记录与统计（默认 7 天）。"""
    from . import history
    from . import disk as diskmod

    days = days if days and days > 0 else 7
    records = history.list_recent_recordings(days=days, limit=200)
    stat = history.stats(days)
    print("===== 最近 %d 天录制记录（%d 条）=====" % (days, len(records)))
    if records:
        print("{:<19} {:<16} {:<10} {:<10} {:<8} {:<4} {}".format(
            "开始时间", "主播", "时长", "大小", "状态", "删除", "文件"))
        print("-" * 106)
        for rec in records:
            started = time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(float(rec.get("started_at") or 0)))
            print("{:<19} {:<16} {:<10} {:<10} {:<8} {:<4} {}".format(
                started,
                _clip(rec.get("anchor") or "", 14),
                diskmod.human_duration(int(rec.get("duration_sec") or 0)),
                diskmod.human_size(int(rec.get("size_bytes") or 0)),
                _clip(rec.get("status") or "", 6),
                "是" if rec.get("deleted") else "否",
                _clip(rec.get("path") or "", 42),
            ))
            if rec.get("error"):
                print("     错误: " + _clip(str(rec["error"]), 100))
    else:
        print("（暂无录制记录）")
    print()
    print("===== 统计 =====")
    for label, key in (("今日", "today"), ("最近 %d 天" % days, "week")):
        item = stat.get(key) or {}
        print("%-12s 录制 %d 次，总时长 %s，总大小 %s，删除 %d 次，失败 %d 次" % (
            label,
            int(item.get("count") or 0),
            diskmod.human_duration(int(item.get("duration_sec") or 0)),
            diskmod.human_size(int(item.get("size_bytes") or 0)),
            int(item.get("deleted") or 0),
            int(item.get("failed") or 0),
        ))
    print("数据库: %s" % history.default_db_path())
    return 0


def cmd_export_diagnostics(config, watchlist, dest: str, days: int = 0) -> int:
    """--export-diagnostics：生成脱敏诊断包（zip）。"""
    from . import history
    from . import disk as diskmod

    if not (dest or "").strip():
        print("请指定诊断包路径，例如：--export-diagnostics D:\\诊断包.zip", file=sys.stderr)
        return 1
    if days is None or days <= 0:
        days = config.get_int("log_keep_days") or 30
    try:
        path = export_diagnostics(dest, config, watchlist, days=days)
    except Exception as exc:
        print("生成诊断包失败: %s" % exc, file=sys.stderr)
        return 1
    size = path.stat().st_size if path.exists() else 0
    print("诊断包已生成: %s（%s）" % (path, diskmod.human_size(size)))
    print("包含: 脱敏 config.json、系统信息、ffmpeg 探测结果、主播数量统计、")
    print("      最近 %d 天日志、history.db 统计摘要。" % days)
    print("提示: cookie/webhook 已打码、路径里的用户名已打码，可直接发给开发者分析。")
    print("历史库: %s" % history.default_db_path())
    return 0


# ---------------------------------------------------------------------------
# 诊断包导出（--export-diagnostics）
# ---------------------------------------------------------------------------
#: 配置里键名包含这些词就视为敏感信息，导出时打码
_SENSITIVE_KEY_RE = re.compile(
    r"(cookie|token|secret|password|passwd|pwd|webhook|authorization|session|signature)", re.I)
#: Windows 用户目录里的用户名打码
_USER_PATH_RE = re.compile(r"([A-Za-z]:\\Users\\)([^\\/:*?\"<>|]+)", re.I)
#: 本工具日志文件名：run-YYYY-MM-DD.log / dyrec-YYYYMMDD.log（含轮转 .1）
_LOG_NAME_RE = re.compile(r"^(?:run-(\d{4}-\d{2}-\d{2})|dyrec-(\d{8}))\.log(?:\.\d+)?$")
#: 单个日志文件在诊断包里的最大字节数（超长只保留尾部）
_LOG_MAX_BYTES = 5 * 1024 * 1024


def _mask_user_path(text: str) -> str:
    """把路径里的 Windows 用户名打码：C:\\Users\\Admin\\... -> C:\\Users\\***\\..."""
    if not text:
        return text
    return _USER_PATH_RE.sub(lambda match: match.group(1) + "***", str(text))


def _redact_config(value, key: str = ""):
    """递归脱敏配置：敏感键打码为 ***，字符串里的用户名打码。"""
    if isinstance(value, dict):
        result = {}
        for sub_key, sub_value in value.items():
            if _SENSITIVE_KEY_RE.search(str(sub_key)):
                result[sub_key] = "***"
            else:
                result[sub_key] = _redact_config(sub_value, str(sub_key))
        return result
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    if isinstance(value, str):
        return _mask_user_path(value)
    return value


def _config_snapshot_text(config) -> str:
    """生成脱敏后的 config.json 文本。"""
    try:
        data = {str(k): v for k, v in dict(config).items() if not str(k).startswith("_")}
    except Exception:
        data = {}
    return json.dumps(_redact_config(data), ensure_ascii=False, indent=2)


def _total_ram_text() -> str:
    """总内存（Windows 用 ctypes 查，其它平台返回未知）。"""
    try:
        import ctypes
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return "%.1f GB" % (status.ullTotalPhys / (1024.0 ** 3))
    except Exception:
        pass
    return "未知"


def _system_info_text() -> str:
    """系统信息（用于诊断）。"""
    import platform
    from .config import ROOT as project_root
    lines = [
        "生成时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "操作系统: %s" % platform.platform(),
        "Python: %s" % platform.python_version(),
        "解释器: %s" % _mask_user_path(sys.executable),
        "CPU 逻辑核心数: %s" % (os.cpu_count() or "未知"),
        "内存: %s" % _total_ram_text(),
        "项目根目录: %s" % _mask_user_path(str(project_root)),
    ]
    return "\n".join(lines)


def _watchlist_summary_text(watchlist) -> str:
    """主播列表摘要（只统计数量，不含名称/链接/房间号）。"""
    try:
        items = watchlist.all()
    except Exception:
        items = []
    total = len(items)
    enabled = sum(1 for item in items if getattr(item, "enabled", True))
    auto = sum(1 for item in items if getattr(item, "auto_record", False))
    quality: dict = {}
    for item in items:
        key = getattr(item, "quality", "") or "origin"
        quality[key] = quality.get(key, 0) + 1
    lines = [
        "主播总数: %d" % total,
        "启用数: %d" % enabled,
        "自动录制数: %d" % auto,
        "画质分布: %s" % ", ".join("%s=%d" % (k, v) for k, v in sorted(quality.items())),
        "（出于隐私，诊断包不包含主播名称/直播链接/房间号）",
    ]
    return "\n".join(lines)


def _log_files(log_dir: Path, days: int) -> list:
    """列出最近 days 天本工具自己的日志文件。"""
    if not log_dir.is_dir():
        return []
    cutoff = time.time() - max(1, int(days or 30)) * 86400
    files = []
    try:
        entries = list(log_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        match = _LOG_NAME_RE.match(entry.name)
        if not match:
            continue
        if match.group(1):
            date_text = match.group(1)
        else:
            digits = match.group(2)
            date_text = "%s-%s-%s" % (digits[0:4], digits[4:6], digits[6:8])
        try:
            stamp = time.mktime(time.strptime(date_text, "%Y-%m-%d"))
        except ValueError:
            continue
        if stamp >= cutoff:
            files.append(entry)
    files.sort(key=lambda p: p.name)
    return files


def _read_masked_log(path: Path, max_bytes: int = _LOG_MAX_BYTES) -> str:
    """读取日志文件内容并打码用户名；超长只保留尾部。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return "（读取日志失败: %s）" % exc
    cut = False
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        cut = True
    text = data.decode("utf-8", "replace")
    if cut:
        text = "……（日志过大，仅保留最后 %d MB）……\n" % (max_bytes // (1024 * 1024)) + text
    return _mask_user_path(text)


def _default_diag_name() -> str:
    return "diagnostics-" + time.strftime("%Y%m%d-%H%M%S") + ".zip"


def export_diagnostics(dest, config, watchlist, days: int = 0, log_dir=None) -> Path:
    """生成脱敏诊断包（zip），返回 zip 绝对路径。

    内容：脱敏 config.json、系统信息、ffmpeg 探测结果、主播数量统计、
         history.db 统计摘要、最近 days 天日志（用户名已打码）。
    """
    import zipfile
    from . import ffmpeg_util
    from . import history

    if days is None or days <= 0:
        days = config.get_int("log_keep_days") or 30
    destination = Path(dest)
    if destination.exists() and destination.is_dir():
        destination = destination / _default_diag_name()
    elif not destination.suffix:
        destination = destination.with_suffix(".zip")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError("无法创建诊断包目录 %s: %s" % (destination.parent, exc))

    logs_dir = Path(log_dir) if log_dir else config.log_dir_path
    log_files = _log_files(logs_dir, days)

    ffmpeg_ok, ffmpeg_path, ffmpeg_info = ffmpeg_util.check_ffmpeg(config.get_str("ffmpeg_path"))
    ffmpeg_lines = [
        "配置 ffmpeg_path: %s" % (_mask_user_path(config.get_str("ffmpeg_path")) or "（空，自动探测）"),
        "解析结果: %s" % ("成功" if ffmpeg_ok else "未找到"),
        "路径: %s" % (_mask_user_path(ffmpeg_path) if ffmpeg_path else "-"),
        "版本/说明: %s" % _mask_user_path(str(ffmpeg_info)),
        "",
        "候选链（前 10 个）:",
    ]
    for candidate in ffmpeg_util.iter_ffmpeg_candidates(config.get_str("ffmpeg_path"))[:10]:
        ffmpeg_lines.append("  - %s" % _mask_user_path(str(candidate)))

    history_payload = {
        "days": days,
        "stats": history.stats(days),
        "recent_errors": [
            {
                "ts": item.get("ts"),
                "source": item.get("source"),
                "message": _mask_user_path(str(item.get("message") or "")),
            }
            for item in history.list_recent_errors(days=days, limit=50)
        ],
    }
    readme = "\n".join([
        "抖音直播录制工具 - 诊断包",
        "生成时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "日志天数: %d" % days,
        "",
        "文件说明：",
        "  config.json          配置快照（已脱敏：cookie/webhook 等敏感值打码，路径用户名打码）",
        "  system.txt           操作系统 / Python / CPU / 内存 / 项目路径",
        "  ffmpeg.txt           ffmpeg 探测结果与候选链",
        "  watchlist_summary.txt 主播数量统计（不含名称/链接/房间号）",
        "  history_stats.json   history.db 统计摘要与最近错误（不含录像内容）",
        "  logs/                最近 %d 天日志（路径中的用户名已打码）" % days,
        "",
        "注意：诊断包不含录像文件、不含 cookie/webhook 明文，可直接发给开发者分析。",
    ])

    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.txt", readme)
            archive.writestr("config.json", _config_snapshot_text(config))
            archive.writestr("system.txt", _system_info_text())
            archive.writestr("ffmpeg.txt", "\n".join(ffmpeg_lines))
            archive.writestr("watchlist_summary.txt", _watchlist_summary_text(watchlist))
            archive.writestr("history_stats.json",
                             json.dumps(history_payload, ensure_ascii=False, indent=2))
            for log_path in log_files:
                archive.writestr("logs/%s" % log_path.name, _read_masked_log(log_path))
    except OSError as exc:
        raise OSError("生成诊断包失败: %s" % exc)
    return destination.resolve()

