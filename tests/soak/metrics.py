# -*- coding: utf-8 -*-
"""浸泡测试指标采集（Windows）：RSS / 句柄 / 线程 / CPU / 请求与业务指标。

每 interval 秒采样一次写 CSV；每 summary_every 秒输出一行摘要（均值/峰值）。
提供 summary() 返回全程均值/最大值/趋势，供报告使用。
"""
from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

LOG = logging.getLogger("soak.metrics")

# CSV 固定列（provider 必须提供其中的业务列）
FIELDS = [
    "ts", "elapsed_s", "rss_mb", "cpu_pct", "handles", "py_threads",
    "ffmpeg_procs", "req_total", "req_ok", "req_err", "req_success_rate",
    "live_rooms", "recordings", "checks_done", "policy_queue", "disk_mb",
    "disk_files", "remux_ok", "remux_fail", "events_started", "events_stopped",
    "queued_count", "risk_count", "avg_latency_ms", "in_flight", "breaker_paused",
]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


TH32CS_SNAPPROCESS = 0x00000002


class WinProcessMetrics:
    """通过 ctypes 读取当前进程的 RSS / CPU / 句柄 / 子进程数（仅 Windows）。"""

    def __init__(self) -> None:
        self.enabled = os.name == "nt"
        self._last_cpu = 0.0
        self._last_wall = 0.0
        self._pid = os.getpid()
        if self.enabled:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
            self.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            self.kernel32.GetProcessTimes.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
            ]
            self.kernel32.GetProcessTimes.restype = wt.BOOL
            self.psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
            ]
            self.psapi.GetProcessMemoryInfo.restype = wt.BOOL
            self.kernel32.GetProcessHandleCount.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(wt.DWORD),
            ]
            self.kernel32.GetProcessHandleCount.restype = wt.BOOL
            self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            self._proc_handle = self.kernel32.GetCurrentProcess()

    def _cpu_seconds(self) -> float:
        creation = wt.FILETIME()
        exit_time = wt.FILETIME()
        kernel = wt.FILETIME()
        user = wt.FILETIME()
        ok = self.kernel32.GetProcessTimes(
            self._proc_handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return 0.0

        def _to_u64(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        return (_to_u64(kernel) + _to_u64(user)) / 1e7

    def _rss_mb(self) -> float:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        ok = self.psapi.GetProcessMemoryInfo(
            self._proc_handle, ctypes.byref(counters), counters.cb,
        )
        if not ok:
            return 0.0
        return round(counters.WorkingSetSize / 1048576.0, 2)

    def _handles(self) -> int:
        count = wt.DWORD(0)
        ok = self.kernel32.GetProcessHandleCount(self._proc_handle, ctypes.byref(count))
        return int(count.value) if ok else -1

    def _child_procs(self) -> int:
        """统计当前进程的子进程数（ffmpeg 录制进程）。"""
        snapshot = self.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot in (0, -1, 0xFFFFFFFF):
            return -1
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        count = 0
        try:
            ok = self.kernel32.Process32First(snapshot, ctypes.byref(entry))
            while ok:
                if entry.th32ParentProcessID == self._pid:
                    count += 1
                ok = self.kernel32.Process32Next(snapshot, ctypes.byref(entry))
        finally:
            self.kernel32.CloseHandle(snapshot)
        return count

    def sample(self) -> Dict[str, float]:
        """返回 rss_mb / cpu_pct / handles / ffmpeg_procs（非 Windows 返回 0）。"""
        now = time.time()
        if not self.enabled:
            return {"rss_mb": 0.0, "cpu_pct": 0.0, "handles": -1, "ffmpeg_procs": 0}
        cpu = self._cpu_seconds()
        cpu_pct = 0.0
        if self._last_wall and now > self._last_wall:
            cpu_pct = round((cpu - self._last_cpu) / (now - self._last_wall) * 100.0, 2)
        self._last_cpu = cpu
        self._last_wall = now
        return {
            "rss_mb": self._rss_mb(),
            "cpu_pct": max(0.0, cpu_pct),
            "handles": self._handles(),
            "ffmpeg_procs": self._child_procs(),
        }


def dir_stats(path: Path) -> Dict[str, float]:
    """递归统计目录下文件数与总大小（MB）。"""
    files = 0
    total = 0
    path = Path(path)
    if not path.exists():
        return {"disk_mb": 0.0, "disk_files": 0}
    try:
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                    files += 1
                except OSError:
                    pass
    except OSError:
        pass
    return {"disk_mb": round(total / 1048576.0, 2), "disk_files": files}


class MetricsRecorder:
    """周期采样器：CSV + 每 10 分钟摘要 + 结束时汇总。"""

    def __init__(self, csv_path: Path, interval: float = 10.0,
                 provider: Optional[Callable[[], Dict[str, float]]] = None,
                 summary_every: float = 600.0,
                 logger: Optional[logging.Logger] = None) -> None:
        self.csv_path = Path(csv_path)
        self.interval = max(1.0, float(interval))
        self.provider = provider
        self.summary_every = max(60.0, float(summary_every))
        self.log = logger or LOG
        self.rows: List[Dict] = []
        self.summaries: List[Dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at = 0.0
        self._proc = WinProcessMetrics()
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.csv_path, "w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDS, extrasaction="ignore")
        self._writer.writeheader()

    def start(self) -> None:
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._loop, name="soak-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(15)
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass

    def _sample(self) -> Dict:
        row: Dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        row["elapsed_s"] = round(time.time() - self._started_at, 1)
        row.update(self._proc.sample())
        row["py_threads"] = threading.active_count()
        if self.provider is not None:
            try:
                row.update(self.provider())
            except Exception as exc:
                self.log.warning("指标 provider 异常: %s", exc)
        err = int(row.get("req_err") or 0)
        total = int(row.get("req_total") or 0)
        row["req_success_rate"] = round((total - err) / total * 100.0, 2) if total else 0.0
        return row

    def _loop(self) -> None:
        next_summary = self._started_at + self.summary_every
        while not self._stop.is_set():
            row = self._sample()
            self.rows.append(row)
            try:
                self._writer.writerow(row)
                self._file.flush()
            except Exception as exc:
                self.log.warning("写 CSV 失败: %s", exc)
            now = time.time()
            if now >= next_summary:
                next_summary = now + self.summary_every
                summary = self.rolling_summary()
                self.summaries.append(summary)
                self.log.info("摘要: %s", summary)
                print("[metrics] " + str(summary), flush=True)
            self._stop.wait(self.interval)

    @staticmethod
    def _agg(rows: List[Dict], key: str) -> Dict[str, float]:
        vals = []
        for row in rows:
            try:
                vals.append(float(row.get(key) or 0))
            except (TypeError, ValueError):
                pass
        if not vals:
            return {"mean": 0.0, "max": 0.0, "min": 0.0}
        return {"mean": round(sum(vals) / len(vals), 2), "max": round(max(vals), 2),
                "min": round(min(vals), 2)}

    def rolling_summary(self) -> Dict:
        rows = self.rows[-360:]
        if not rows:
            return {}
        last = rows[-1]
        return {
            "t": last["ts"],
            "elapsed_s": last["elapsed_s"],
            "rss_mb": self._agg(rows, "rss_mb"),
            "cpu_pct": self._agg(rows, "cpu_pct"),
            "handles": self._agg(rows, "handles"),
            "py_threads": self._agg(rows, "py_threads"),
            "ffmpeg_procs": self._agg(rows, "ffmpeg_procs"),
            "recording": last.get("recordings"),
            "live_rooms": last.get("live_rooms"),
            "req_total": last.get("req_total"),
            "success_rate": last.get("req_success_rate"),
            "disk_mb": last.get("disk_mb"),
            "remux_ok": last.get("remux_ok"),
            "remux_fail": last.get("remux_fail"),
        }

    def summary(self) -> Dict:
        if not self.rows:
            return {}
        first = self.rows[0]
        last = self.rows[-1]
        duration_h = max(0.001, float(last.get("elapsed_s") or 0) / 3600.0)
        rss_first = float(first.get("rss_mb") or 0)
        rss_last = float(last.get("rss_mb") or 0)
        return {
            "samples": len(self.rows),
            "duration_h": round(duration_h, 3),
            "rss_mb": self._agg(self.rows, "rss_mb"),
            "cpu_pct": self._agg(self.rows, "cpu_pct"),
            "handles": self._agg(self.rows, "handles"),
            "py_threads": self._agg(self.rows, "py_threads"),
            "ffmpeg_procs": self._agg(self.rows, "ffmpeg_procs"),
            "recordings": self._agg(self.rows, "recordings"),
            "live_rooms": self._agg(self.rows, "live_rooms"),
            "disk_mb": self._agg(self.rows, "disk_mb"),
            "queued_count": self._agg(self.rows, "queued_count"),
            "risk_count": self._agg(self.rows, "risk_count"),
            "avg_latency_ms": self._agg(self.rows, "avg_latency_ms"),
            "in_flight": self._agg(self.rows, "in_flight"),
            "breaker_paused": self._agg(self.rows, "breaker_paused"),
            "rss_first_mb": rss_first,
            "rss_last_mb": rss_last,
            "rss_drift_mb": round(rss_last - rss_first, 2),
            "rss_drift_mb_per_h": round((rss_last - rss_first) / duration_h, 2),
            "req_total": last.get("req_total"),
            "req_success_rate": last.get("req_success_rate"),
            "remux_ok": last.get("remux_ok"),
            "remux_fail": last.get("remux_fail"),
            "events_started": last.get("events_started"),
            "events_stopped": last.get("events_stopped"),
            "checks_done": last.get("checks_done"),
        }
