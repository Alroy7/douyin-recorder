# -*- coding: utf-8 -*-
"""系统容量探测与并发/带宽/磁盘保留值推荐。

设计目标：
1. 启动时读取本机 CPU 核心数、内存、磁盘剩余空间、网卡下行带宽；
2. 据此给出「合适」的并发/带宽/磁盘保留推荐值，自动回填到 config.json
   里当前为 0（=自动/不限）的项，绝不覆盖用户显式设置过的值；
3. 保证「能录制所有直播的主播」：
   - max_concurrent 永远保持 0（不按路数硬限，超出的走排队，闸门释放后继续录）；
   - max_total_mbps 只有真正探测到网卡带宽时才设置，探测失败保持 0（不限）；
   - min_free_gb 只在「保留值明显不合理（>= 磁盘剩余的一半）」时才自动下调，
     避免磁盘一满就反复停录/重开的死循环。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict, Optional

from .disk import free_gb as _disk_free_gb

LOG = logging.getLogger("dyrec.capacity")

#: 探测命令超时（秒），避免启动卡住
_DETECT_TIMEOUT = 6.0
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def cpu_cores() -> int:
    """逻辑 CPU 核心数（失败回退 4）。"""
    try:
        n = os.cpu_count()
        return max(1, int(n)) if n else 4
    except Exception:  # pragma: no cover
        return 4


def ram_gb() -> float:
    """物理内存 GB（失败返回 0，表示未知）。"""
    if os.name == "nt":
        try:
            import ctypes
            class _MEMORYSTATUSEX(ctypes.Structure):
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
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            pass
        return 0.0
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / (1024 ** 3), 1)
    except Exception:
        return 0.0


def _parse_bps(text: str) -> Optional[int]:
    """把网卡速度字符串解析成 Mbps（输入可能是 bps 或 '1 Gbps' 之类）。"""
    if not text:
        return None
    s = str(text).strip().lower()
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*(gbps|mbps|kbps|bps|g|m|k|b)?", s)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").strip()
    if unit in ("gbps", "g"):
        value *= 1000
    elif unit in ("kbps", "k"):
        value /= 1000
    elif unit in ("bps", "b") or (not unit and value > 100000):
        value /= 1000_000
    if value <= 0 or value > 100_000:
        return None
    return int(round(value))


def network_downlink_mbps() -> Optional[int]:
    """探测下行带宽（Mbps）。

    顺序：
      1) PowerShell Get-NetAdapter 的 LinkSpeed（本机链路速度，作为上限参考）；
      2) wmic NIC get Speed（老系统兜底）；
    失败返回 None（调用方保持「不限」以保证能录制所有主播）。
    """
    if os.name != "nt":
        return None
    best: Optional[int] = None

    try:
        ps = (
            "Get-NetAdapter -Physical -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Status -eq 'Up' } | "
            "Select-Object -ExpandProperty LinkSpeed"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=_DETECT_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode == 0:
            for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
                mbps = _parse_bps(line)
                if mbps:
                    best = mbps if best is None else max(best, mbps)
    except Exception:  # pragma: no cover - 探测失败不致命
        pass

    if best is None:
        try:
            proc = subprocess.run(
                ["wmic", "NIC", "where", "NetEnabled=true", "get", "Speed", "/value"],
                capture_output=True, timeout=_DETECT_TIMEOUT,
                creationflags=_CREATE_NO_WINDOW,
            )
            if proc.returncode == 0:
                for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
                    if "=" in line:
                        mbps = _parse_bps(line.split("=", 1)[1])
                        if mbps:
                            best = mbps if best is None else max(best, mbps)
        except Exception:  # pragma: no cover
            pass

    return best


def detect() -> Dict[str, Any]:
    """原始探测结果（便于测试/诊断展示）。"""
    return {
        "cpu_cores": cpu_cores(),
        "ram_gb": ram_gb(),
        "network_mbps": network_downlink_mbps(),
    }


def recommend(cfg, free_gb: Optional[float] = None,
              network_mbps: Optional[int] = None,
              cores: Optional[int] = None) -> Dict[str, Any]:
    """根据本机配置给出推荐值（纯计算、不落盘，便于测试注入假探测值）。

    network_mbps 传 None 表示「未探测到带宽」；cores 传 None 表示用 cpu_cores()。
    返回 dict，值为 None 表示「不调整」。
    """
    if cores is None:
        cores = cpu_cores()
    if free_gb is None:
        try:
            free_gb = _disk_free_gb(cfg.output_root)
        except Exception:
            free_gb = -1.0

    notes: list = []
    rec: Dict[str, Any] = {
        "max_total_mbps": None,
        "max_ffmpeg_processes": None,
        "max_poll_workers": None,
        "remux_concurrency": None,
        "min_free_gb": None,
        "notes": notes,
    }

    # ---- 带宽预算：只有真正探测到才设；按 75% 预留，避免自我打满 ----
    if network_mbps and network_mbps > 0:
        budget = max(50, int(network_mbps * 0.75))
        rec["max_total_mbps"] = budget
        notes.append("检测到网络下行约 %d Mbps，带宽预算推荐 %d Mbps（约 %d 路原画）"
                     % (network_mbps, budget, max(1, budget // 6)))
    else:
        notes.append("未能探测网络带宽，max_total_mbps 保持 0（不限），以保证所有开播主播都能录制")

    # ---- 进程/线程/转码并发：按 CPU 给宽松上限，仅作防失控，不限制正常录制 ----
    rec["max_ffmpeg_processes"] = max(64, min(1024, cores * 8))
    rec["max_poll_workers"] = max(4, min(32, cores * 2))
    rec["remux_concurrency"] = max(2, min(8, max(1, cores // 2)))

    # ---- 磁盘保留值：仅当「保留值 >= 剩余空间的一半」时自动下调（典型病态配置）----
    try:
        current = float(cfg.get_float("min_free_gb"))
    except Exception:
        current = 0.0
    if free_gb > 0 and current > 0 and current >= free_gb * 0.5:
        new_reserve = round(max(2.0, min(10.0, free_gb * 0.15)), 1)
        rec["min_free_gb"] = new_reserve
        notes.append("磁盘剩余 %.1f GB，但 min_free_gb=%.1f 过于激进，已自动下调为 %.1f GB"
                     "，避免反复停录/重开" % (free_gb, current, new_reserve))

    return rec


def apply_recommendations(cfg) -> Dict[str, Any]:
    """启动时按探测结果回填「自动/不限」的配置项并落盘。

    只改当前为 0（=自动）的项；min_free_gb 仅在病态时下调；max_concurrent 永不改。
    返回 {"applied": {...}, "notes": [...]}。
    """
    if not cfg.get_bool("auto_tune_limits", True):
        return {"applied": {}, "notes": ["auto_tune_limits 已关闭，跳过启动自动调优"]}

    # 仅当 max_total_mbps 仍是「自动」时才探测网卡（避免每次启动都跑 powershell）
    need_net = cfg.get_int("max_total_mbps") == 0
    network_mbps = network_downlink_mbps() if need_net else None
    rec = recommend(cfg, network_mbps=network_mbps)

    applied: Dict[str, Any] = {}
    for key in ("max_total_mbps", "max_ffmpeg_processes", "max_poll_workers",
                "remux_concurrency"):
        val = rec.get(key)
        if val is None:
            continue
        try:
            cur = cfg.get_int(key)
        except Exception:
            cur = 0
        if cur == 0:
            cfg[key] = int(val)
            applied[key] = int(val)
    if rec.get("min_free_gb") is not None:
        cfg["min_free_gb"] = float(rec["min_free_gb"])
        applied["min_free_gb"] = float(rec["min_free_gb"])

    if applied:
        try:
            cfg.save()
        except Exception as exc:  # pragma: no cover
            LOG.warning("保存自动调优结果失败: %s", exc)
    for note in rec.get("notes", []):
        LOG.info("[容量调优] %s", note)
    if applied:
        LOG.info("[容量调优] 已应用: %s", applied)
    return {"applied": applied, "notes": rec.get("notes", [])}
