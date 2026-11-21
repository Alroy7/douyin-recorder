# -*- coding: utf-8 -*-
"""监控调度：自适应轮询、限速、熔断、并发闸门、录制启停、事件分发。

P1 架构（面向 1000+ 主播长期挂机）：
- 优先队列（小根堆）+ 固定工作池（max_poll_workers，默认 12）：每个房间按自己的
  到期时间入队，调度线程只派发到固定线程池，不再「每 tick 全量提交」；
- 全局令牌桶限速（poll_rate_limit 次/秒，±20% 抖动），防止 1000+ 房间把接口打爆；
- 自适应轮询档位：直播中 30s / 24h 内开播过 90s / 长期未开播 300s；
  单房间查询失败指数退避 30s -> 5min；只有状态变化才写 INFO 日志；
- 全局熔断：连续 2 个统计窗口错误率 > 30% 时暂停 30~120s（指数），期间强制刷新
  会话（dy_api.get_cookie(force=True)），随后半开逐步恢复；
- 批量导入解析队列：固定 2~4 个解析线程 + 半速令牌桶，导入 1360 条时线程数稳定；
- 并发闸门：max_concurrent（0=不限）/ max_total_mbps / max_ffmpeg_processes /
  磁盘剩余空间；超限的房间状态 = 排队中（queued=True），闸门释放后自动开录。

录制线程由 RoomRecorder 自己管理，Manager 只负责启动/停止和状态汇总；
GUI 通过 snapshot() 拉取状态、通过 on_event 回调接收事件。
"""
from __future__ import annotations

import heapq
import logging
import queue
import random
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import disk as diskmod
from . import ffmpeg_util
from . import history as historymod
from .checker import LiveChecker, StatusView
from .model import (
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_LIVE,
    STATUS_OFFLINE,
    STATUS_RECONNECTING,
    STATUS_RECORDING,
    EventType,
    RecordEvent,
    RoomRuntime,
    WatchItem,
    render_filename,
    render_output_dir,
)
from .notify import Notifier
from .recorder import RoomRecorder
from .storage import Watchlist

LOG = logging.getLogger("dyrec.manager")

#: 排队状态（在录/开播等状态常量之外，按 P1 需求新增；不改 model.py）
STATUS_QUEUED = "排队中"
#: 兼容旧模块常量（老代码引用过）
MAX_CHECK_BACKOFF = 600
#: 24 小时（判定「最近开播过」的窗口）
WARM_WINDOW_SECONDS = 86400

#: 按选择清晰度估算单路码率（Mbps）：origin/uhd 6.4、hd 2、sd 1
QUALITY_MBPS: Dict[str, float] = {
    "origin": 6.4,
    "uhd": 6.4,
    "best": 6.4,
    "原画": 6.4,
    "hd": 2.0,
    "高清": 2.0,
    "sd": 1.0,
    "标清": 1.0,
}
#: 估算不到时的保守值
DEFAULT_MBPS = 6.4

def _status_sec_uid(st) -> str:
    """从一次查询结果里取主播 sec_uid（dy_api 把 owner.sec_uid 放在 raw 里）。"""
    try:
        obj = getattr(st, "raw_obj", None)
        raw = getattr(obj, "raw", None)
        if isinstance(raw, dict):
            sec = raw.get("sec_uid")
            if sec:
                return str(sec).strip()
    except Exception:  # pragma: no cover
        pass
    return ""


def _looks_like_disconnect(message: str) -> bool:
    """RECONNECT 事件里是否属于真正的断流/卡死（"第 N 次重连" 只是重试开始标记，不重复计数）。

    注意：不计入「启动后 N 秒未收到任何数据」——那是拉流地址过期/失效的症状，
    由重连时重新取地址解决，不应触发清晰度降档（否则会把原画误降成 hd/sd）。
    """
    text = str(message or "")
    return ("断流" in text) or ("卡死" in text)


#: 错误文本里的「风控/限流」关键词（用于统计 risk_count）
RISK_ERROR_PATTERNS = (
    "风控", "验证", "验证码", "限流", "频率", "429", "403", "captcha",
    "risk", "too many", "blocked", "forbidden",
)


def estimate_mbps(quality: str) -> float:
    """按清晰度估算单路录制码率（Mbps）。"""
    key = str(quality or "origin").strip().lower()
    return float(QUALITY_MBPS.get(key, DEFAULT_MBPS))


def _looks_like_risk(message: str) -> bool:
    """错误信息是否像风控/限流。"""
    text = str(message or "").lower()
    return any(token in text for token in RISK_ERROR_PATTERNS)


class TokenBucket:
    """令牌桶限速（线程安全）。

    - rate：每秒令牌数；burst：桶容量（默认 = max(1, rate)）；
    - jitter：每次补充令牌时速率乘 1±jitter 的随机抖动（默认 ±20%）；
    - clock：可注入时钟（默认 time.monotonic），便于单元测试。
    """

    def __init__(self, rate: float, burst: Optional[float] = None,
                 jitter: float = 0.2, clock: Optional[Callable[[], float]] = None,
                 rng: Optional[random.Random] = None):
        self.rate = max(0.001, float(rate))
        self.burst = max(1.0, float(burst) if burst is not None else max(1.0, self.rate))
        self.jitter = max(0.0, min(0.9, float(jitter)))
        self._clock = clock or time.monotonic
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._tokens = self.burst
        self._last = self._clock()

    def _refill_locked(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed > 0:
            factor = 1.0
            if self.jitter > 0:
                factor = 1.0 + self._rng.uniform(-self.jitter, self.jitter)
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate * factor)
        self._last = max(self._last, now)

    def try_acquire(self, now: Optional[float] = None) -> bool:
        """尝试取一个令牌（不阻塞）。"""
        now = self._clock() if now is None else float(now)
        with self._lock:
            self._refill_locked(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def wait_time(self, now: Optional[float] = None) -> float:
        """距离下一个令牌可用的秒数（0 表示现在就有）。"""
        now = self._clock() if now is None else float(now)
        with self._lock:
            self._refill_locked(now)
            if self._tokens >= 1.0:
                return 0.0
            return max(0.0, (1.0 - self._tokens) / self.rate)

    def acquire(self, stop_event: Optional[threading.Event] = None,
                timeout: Optional[float] = None) -> bool:
        """阻塞等待一个令牌；stop_event 置位或超时返回 False。"""
        deadline = None if timeout is None else self._clock() + float(timeout)
        while True:
            if self.try_acquire():
                return True
            if stop_event is not None and stop_event.is_set():
                return False
            if deadline is not None and self._clock() >= deadline:
                return False
            wait_seconds = min(0.1, max(0.005, self.wait_time()))
            if stop_event is not None:
                stop_event.wait(wait_seconds)
            else:
                time.sleep(wait_seconds)


class CircuitBreaker:
    """全局熔断器（查询错误率过高时暂停并半开恢复）。

    统计窗口（window_seconds，默认 30s）内错误率 > error_rate（默认 30%）时
    记一次「高错误窗口」；连续 2 个高错误窗口 -> 熔断：暂停 pause_start 秒
    （之后指数增长，封顶 pause_max）；暂停结束后进入半开状态，放行
    halfopen_requests 个请求，全部成功则恢复，任一失败则再次熔断（暂停翻倍）。
    """

    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"

    def __init__(self, error_rate: float = 0.3, window_seconds: float = 30.0,
                 min_requests: int = 20, pause_start: float = 30.0,
                 pause_max: float = 120.0, halfopen_requests: int = 5,
                 clock: Optional[Callable[[], float]] = None):
        self.error_rate = max(0.01, min(1.0, float(error_rate)))
        self.window_seconds = max(1.0, float(window_seconds))
        self.min_requests = max(1, int(min_requests))
        self.pause_start = max(1.0, float(pause_start))
        self.pause_max = max(self.pause_start, float(pause_max))
        self.halfopen_requests = max(1, int(halfopen_requests))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()

        self.state = self.CLOSED
        self.total_requests = 0
        self.total_errors = 0
        self.total_latency = 0.0
        self.risk_count = 0
        self._consecutive_high = 0
        self._pause_seconds = 0.0
        self._open_until = 0.0
        self._open_count = 0
        self._halfopen_left = 0
        self._window_start = self._clock()
        self._window_requests = 0
        self._window_errors = 0

    # ---------------- 查询 ----------------
    @property
    def pause_seconds(self) -> float:
        return self._pause_seconds

    def paused_until(self) -> float:
        """当前熔断暂停结束时间（monotonic；未熔断返回 0）。"""
        return self._open_until if self.state == self.OPEN else 0.0

    def allow(self, now: Optional[float] = None) -> bool:
        """是否允许发送下一次查询（会触发 OPEN -> HALF_OPEN 的状态迁移）。"""
        now = self._clock() if now is None else float(now)
        with self._lock:
            if self.state == self.OPEN:
                if now >= self._open_until:
                    self.state = self.HALF_OPEN
                    self._halfopen_left = self.halfopen_requests
                    return True
                return False
            if self.state == self.HALF_OPEN:
                return self._halfopen_left > 0
            return True

    def record(self, success: bool, latency: float = 0.0,
               now: Optional[float] = None, risk: bool = False) -> None:
        """记录一次查询结果，驱动窗口统计与状态迁移。"""
        now = self._clock() if now is None else float(now)
        with self._lock:
            self.total_requests += 1
            self.total_latency += float(latency or 0)
            if risk:
                self.risk_count += 1
            if not success:
                self.total_errors += 1
            if self.state == self.OPEN:
                # 暂停期间理论不派发，忽略窗口
                self._window_start = now
                self._window_requests = 0
                self._window_errors = 0
                return
            self._window_requests += 1
            if not success:
                self._window_errors += 1

            if self.state == self.HALF_OPEN:
                if success:
                    self._halfopen_left -= 1
                    if self._halfopen_left <= 0:
                        self._close(now)
                else:
                    self._open(now, reopen=True)
                return

            # CLOSED：窗口到期后判定
            if (now - self._window_start) >= self.window_seconds:
                if self._window_requests >= self.min_requests:
                    rate = self._window_errors / float(self._window_requests)
                    if rate > self.error_rate:
                        self._consecutive_high += 1
                    else:
                        self._consecutive_high = 0
                    if self._consecutive_high >= 2:
                        self._open(now, reopen=False)
                        return
                self._window_start = now
                self._window_requests = 0
                self._window_errors = 0

    def _open(self, now: float, reopen: bool) -> None:
        if reopen:
            self._pause_seconds = min(self.pause_max,
                                      max(self._pause_seconds * 2.0, self.pause_start))
        else:
            self._pause_seconds = self.pause_start
        self._open_count += 1
        self.state = self.OPEN
        self._open_until = now + self._pause_seconds
        self._halfopen_left = 0
        self._consecutive_high = 0
        self._window_start = now
        self._window_requests = 0
        self._window_errors = 0

    def _close(self, now: float) -> None:
        self.state = self.CLOSED
        self._consecutive_high = 0
        self._open_count = 0
        self._pause_seconds = 0.0
        self._open_until = 0.0
        self._halfopen_left = 0
        self._window_start = now
        self._window_requests = 0
        self._window_errors = 0

    def stats(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if now is None else float(now)
        with self._lock:
            total = self.total_requests
            return {
                "state": self.state,
                "requests": total,
                "success_rate": round(1.0 - (self.total_errors / total), 4) if total else 0.0,
                "avg_latency_ms": round(self.total_latency / total * 1000.0, 1) if total else 0.0,
                "risk_count": self.risk_count,
                "pause_seconds": self._pause_seconds,
                "pause_remaining": round(max(0.0, self._open_until - now), 1) if self.state == self.OPEN else 0.0,
            }


# ---- 查询错误分类：区分「当前没开播」与「条目数据有问题」 ----
_OFFLINE_ERROR_HINTS = (
    "enter 接口无房间数据", "无房间数据", "已解析到主播", "无直播间信息",
    "无法从主页解析", "未开播", "主播当前没有直播", "no room data",
)
_INVALID_ERROR_HINTS = (
    "无法识别的目标", "房间不存在", "web_rid 非法", "不是有效", "invalid",
)


def classify_lookup_error(message: str) -> str:
    """offline = 当前没直播（按未开播处理）；invalid = 条目数据问题；error = 网络/风控。"""
    msg = message or ""
    low = msg.lower()
    if any(h in msg or h in low for h in _OFFLINE_ERROR_HINTS):
        return "offline"
    if any(h in msg or h in low for h in _INVALID_ERROR_HINTS):
        return "invalid"
    return "error"


def describe_lookup_error(message: str) -> str:
    msg = message or ""
    if "无法识别的目标" in msg:
        return "条目无效：只有昵称，没有房间号/主页链接，请重新导入带链接的列表"
    if "房间不存在" in msg or "web_rid 非法" in msg:
        return "房间号无效：请检查该主播填写的房间号"
    return msg


class MonitorManager:
    """监控 + 录制调度器。一个实例管理整个 watchlist。"""

    def __init__(self, config, watchlist: Watchlist,
                 on_event: Optional[Callable[[RecordEvent], None]] = None,
                 logger: Optional[logging.Logger] = None):
        self.cfg = config
        self.watchlist = watchlist
        self.log = logger or LOG
        self.checker = LiveChecker(config)
        self.notifier = Notifier(config, logger=self.log)
        self._on_event = on_event

        self._lock = threading.RLock()
        self._states: Dict[str, RoomRuntime] = {}
        self._last_status: Dict[str, StatusView] = {}
        self._stream_urls: Dict[str, str] = {}   # 本次直播的取流地址（详情预览用）
        self._recorders: Dict[str, RoomRecorder] = {}
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ffmpeg = ""
        self._started_at = 0.0
        self._pending_cleanup = False
        self._disk_blocked: bool = False

        # ---- P1 调度状态（优先队列 + 固定工作池）----
        self._heap: List[Tuple[float, int, str]] = []
        self._heap_seq = 0
        self._scheduled: Dict[str, float] = {}
        self._in_flight: Set[str] = set()
        self._futures: Dict[Future, Tuple[WatchItem, float]] = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        self._last_live_at: Dict[str, float] = {}
        self._poll_stats = {"requests": 0, "success": 0, "failed": 0,
                            "risk": 0, "latency": 0.0}
        self._breaker = self._build_breaker()
        self._poll_bucket = self._build_poll_bucket()
        self._resolve_bucket = self._build_resolve_bucket()

        # ---- 并发闸门 / 排队 ----
        self._queue: Dict[str, Dict[str, Any]] = {}
        self._manual_queue: Set[str] = set()
        self._recorder_mbps: Dict[str, float] = {}
        # ---- 清晰度 / 断流自动降档（P1-E）----
        self._session_quality: Dict[str, str] = {}      # 本场录制当前有效清晰度
        self._session_reconnects: Dict[str, int] = {}   # 本场累计重连/卡死次数
        self._degrade_restart: Dict[str, str] = {}      # 降档后待重开的目标清晰度
        self._manual_sessions: Set[str] = set()         # 本场是手动开录（auto_record=False 也要能降档重开）
        self._pending_gate = True
        self._gate_last_at = 0.0
        self._last_cleanup_at = 0.0

        # ---- 批量导入解析队列（固定 2~4 线程）----
        self._resolve_queue: queue.Queue = queue.Queue()
        self._resolve_pending: Set[str] = set()
        self._resolve_threads: List[threading.Thread] = []
        self._save_thread: Optional[threading.Thread] = None
        self._save_event = threading.Event()
        self._watchlist_dirty = False
        self._workers_stop = threading.Event()
        self._workers_stop.set()
        self._workers_started = False

    # ==================================================================
    # 组件构造（每次 start() 按最新配置重建）
    # ==================================================================
    def _build_breaker(self) -> CircuitBreaker:
        return CircuitBreaker(
            error_rate=self.cfg.get_float("breaker_error_rate", 0.3),
            window_seconds=self.cfg.get_float("breaker_window_seconds", 30),
            min_requests=self.cfg.get_int("breaker_min_requests", 20),
            pause_start=self.cfg.get_float("breaker_pause_start", 30),
            pause_max=self.cfg.get_float("breaker_pause_max", 120),
            halfopen_requests=self.cfg.get_int("breaker_halfopen_requests", 5),
        )

    def _build_poll_bucket(self) -> TokenBucket:
        return TokenBucket(self.cfg.get_int("poll_rate_limit", 6))

    def _build_resolve_bucket(self) -> TokenBucket:
        rate = max(0.5, self.cfg.get_int("poll_rate_limit", 6) / 2.0)
        return TokenBucket(rate)

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self) -> Tuple[bool, str]:
        """开始监控。返回 (是否成功, 说明)。"""
        if self.is_running():
            return True, "监控已在运行"
        ok, ffmpeg, info = ffmpeg_util.check_ffmpeg(self.cfg.get_str("ffmpeg_path"))
        if not ok:
            msg = "无法启动监控：" + info
            self.log.error(msg)
            self._emit(EventType.ERROR, msg)
            return False, msg
        self._ffmpeg = ffmpeg
        self.log.info("ffmpeg: %s", info)
        self.cfg.ensure_dirs()
        self._stop_event.clear()
        self._wake_event.clear()
        self._started_at = time.time()
        self._pending_cleanup = False
        self._gate_last_at = 0.0
        self._last_cleanup_at = 0.0
        self._session_quality.clear()
        self._session_reconnects.clear()
        self._degrade_restart.clear()
        self._manual_sessions.clear()
        self._poll_stats = {"requests": 0, "success": 0, "failed": 0,
                            "risk": 0, "latency": 0.0}
        self._breaker = self._build_breaker()
        self._poll_bucket = self._build_poll_bucket()
        self._resolve_bucket = self._build_resolve_bucket()
        try:
            from .recorder import set_remux_concurrency
            set_remux_concurrency(self.cfg.get_int("remux_concurrency"))
        except Exception:
            pass
        self._disk_blocked = False
        try:
            self.apply_disk_policy("启动检查")
        except Exception:
            pass
        self._ensure_workers()
        self._init_scheduler()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()
        self._emit(EventType.INFO,
                   "监控已启动：轮询限速 %d 次/秒，工作线程 %d"
                   % (self.cfg.get_int("poll_rate_limit"),
                      self.cfg.get_int("max_poll_workers")))
        self.log.info("监控已启动：轮询限速 %d/s，%d 个工作线程，%d 个主播",
                      self.cfg.get_int("poll_rate_limit"),
                      self.cfg.get_int("max_poll_workers"), len(self.watchlist))
        self.log.info(
            "并发闸门：max_concurrent=%d（0=不限），max_total_mbps=%d（0=不限），"
            "max_ffmpeg_processes=%d，磁盘保留 %.1f GB",
            self.cfg.get_int("max_concurrent"), self.cfg.get_int("max_total_mbps"),
            self.cfg.get_int("max_ffmpeg_processes"), self.cfg.get_float("min_free_gb"))
        return True, "已开始监控"

    def stop(self, wait: bool = True, timeout: float = 120.0) -> bool:
        """停止监控：终止所有录制并等待收尾（含把录像转成 MP4）。

        wait=True 时会一直等到所有录制线程结束（收尾/转码完成）或超时；
        返回是否全部收尾完成。超时后录制线程仍可在后台继续转换，
        可再调用 wait_finalize() 继续等待。
        """
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            recorders = list(self._recorders.values())
        for rec in recorders:
            try:
                rec.stop("停止监控")
            except Exception:
                self.log.debug("停止录制异常", exc_info=True)
        all_done = True
        if wait:
            deadline = (time.time() + timeout) if timeout and timeout > 0 else None
            last_note = 0.0
            while True:
                pending = [r for r in recorders if r.is_alive()]
                if not pending:
                    break
                if deadline is not None and time.time() >= deadline:
                    all_done = False
                    break
                if time.time() - last_note >= 5.0:
                    last_note = time.time()
                    self._emit(EventType.INFO,
                               "正在收尾并转换 MP4：剩余 %d 路…" % len(pending))
                    self.log.info("停止监控：等待 %d 路录制收尾/转 MP4", len(pending))
                time.sleep(0.2)
            t = self._thread
            if t is not None and t.is_alive():
                t.join(1.0)
        self._thread = None
        self._shutdown_scheduler()
        self._flush_watchlist()
        self._shutdown_workers()
        if all_done:
            with self._lock:
                self._recorders.clear()
                self._futures.clear()
                self._in_flight.clear()
                self._scheduled.clear()
                self._heap.clear()
            self._emit(EventType.INFO, "监控已停止")
            self.log.info("监控已停止")
        else:
            self._emit(EventType.ERROR,
                       "停止超时：仍有录制在收尾/转换，关闭软件前请等待完成")
            self.log.warning("停止监控超时：仍有录制线程未结束")
        return all_done

    def pending_count(self) -> int:
        """仍在录制/收尾/转换中的录制器数量。"""
        with self._lock:
            return sum(1 for r in self._recorders.values() if r.is_alive())

    def wait_finalize(self, timeout: float = 600.0) -> bool:
        """继续等待剩余录制线程收尾（停止超时后 GUI 可再次调用）。"""
        deadline = (time.time() + timeout) if timeout and timeout > 0 else None
        last_note = 0.0
        while True:
            with self._lock:
                pending = [r for r in self._recorders.values() if r.is_alive()]
            if not pending:
                with self._lock:
                    self._recorders.clear()
                return True
            if deadline is not None and time.time() >= deadline:
                return False
            if time.time() - last_note >= 5.0:
                last_note = time.time()
                self._emit(EventType.INFO,
                           "仍在转换 MP4：剩余 %d 路…" % len(pending))
            time.sleep(0.2)

    def is_running(self) -> bool:
        """监控线程是否在运行。"""
        t = self._thread
        return bool(t is not None and t.is_alive() and not self._stop_event.is_set())

    def _shutdown_scheduler(self) -> None:
        ex = self._executor
        self._executor = None
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # pragma: no cover - 老 Python
                ex.shutdown(wait=False)
            except Exception:  # pragma: no cover
                pass

    # ==================================================================
    # 批量导入解析池 + watchlist 合并保存（导入 1360 条不卡 UI）
    # ==================================================================
    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers_started:
                return
            self._workers_started = True
            self._workers_stop = threading.Event()
            count = max(1, min(16, self.cfg.get_int("resolve_workers") or 3))
            self._resolve_threads = []
            for index in range(count):
                thread = threading.Thread(target=self._resolve_worker_loop,
                                          name="dyresolve-%d" % index, daemon=True)
                thread.start()
                self._resolve_threads.append(thread)
            self._save_thread = threading.Thread(target=self._save_worker_loop,
                                                 name="dysave", daemon=True)
            self._save_thread.start()

    def _shutdown_workers(self) -> None:
        with self._lock:
            if not self._workers_started:
                return
            self._workers_started = False
            self._workers_stop.set()
        for thread in list(self._resolve_threads):
            thread.join(timeout=1.0)
        if self._save_thread is not None:
            self._save_thread.join(timeout=1.0)
        self._resolve_threads = []
        self._save_thread = None

    def _resolve_worker_loop(self) -> None:
        """限速解析 worker：从队列取 (item_id, text, attempt)，按半速令牌桶消费。"""
        retries = max(0, self.cfg.get_int("resolve_retries") or 0)
        while not self._workers_stop.is_set():
            try:
                item_id, text, attempt = self._resolve_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._workers_stop.is_set():
                break
            if not self._resolve_bucket.acquire(stop_event=self._workers_stop):
                continue
            try:
                info = self.checker.resolve(text)
            except Exception as exc:  # pragma: no cover - resolve 内部已兜底
                info = {"error": "解析异常: %s" % exc}
            error = str(info.get("error") or "") if isinstance(info, dict) else "解析返回异常"
            if error:
                if attempt < retries and not self._workers_stop.is_set():
                    self._workers_stop.wait(min(2.0, 0.5 * (2 ** attempt)))
                    self._resolve_queue.put((item_id, text, attempt + 1))
                    continue
                self.log.debug("解析主播信息失败（已重试 %d 次）: %s", attempt, error)
            else:
                self._apply_resolve_result(item_id, info)
            with self._lock:
                self._resolve_pending.discard(item_id)

    def _apply_resolve_result(self, item_id: str, info: Dict[str, Any]) -> None:
        item = self.watchlist.get(item_id)
        if item is None:
            return
        changed = False
        if not item.web_rid and info.get("web_rid"):
            item.web_rid = str(info["web_rid"])
            changed = True
        if not item.url and info.get("url"):
            item.url = str(info["url"])
            changed = True
        if not getattr(item, "sec_uid", "") and info.get("sec_uid"):
            item.sec_uid = str(info["sec_uid"])
            changed = True
        if not item.name and info.get("name"):
            item.name = str(info["name"])
            changed = True
        if changed:
            # save=False + 合并保存：1360 条批量解析时避免每条都写一次 JSON
            self.watchlist.update(item, save=False)
            self._schedule_watchlist_save()
            with self._lock:
                self._state(item.id).updated_at = time.time()
            self._emit(EventType.INFO, "已补全主播信息：%s" % item.display_name, item=item)

    def resolve_pending_count(self) -> int:
        """还有多少个主播信息等待解析（供 GUI/测试观察）。"""
        with self._lock:
            return len(self._resolve_pending)

    def _save_worker_loop(self) -> None:
        """watchlist 合并保存：短时间内批量添加只落盘一次。"""
        while not self._workers_stop.is_set():
            if not self._save_event.wait(0.5):
                continue
            self._save_event.clear()
            self._workers_stop.wait(0.35)   # 合并 0.35s 内的连续添加
            self._flush_watchlist()

    def _schedule_watchlist_save(self) -> None:
        with self._lock:
            self._watchlist_dirty = True
        self._save_event.set()

    def _flush_watchlist(self) -> None:
        with self._lock:
            dirty = self._watchlist_dirty
            self._watchlist_dirty = False
        if dirty:
            try:
                self.watchlist.save()
            except Exception as exc:  # pragma: no cover - 保存失败不致命
                self.log.warning("保存主播列表失败: %s", exc)

    def flush(self) -> None:
        """立即落盘主播列表（GUI 退出/导入结束时可调用）。"""
        self._flush_watchlist()

    # ==================================================================
    # 状态快照
    # ==================================================================
    def _state(self, item_id: str) -> RoomRuntime:
        rt = self._states.get(item_id)
        if rt is None:
            rt = RoomRuntime(item_id=item_id)
            self._states[item_id] = rt
        return rt

    def state_of(self, item_id: str) -> RoomRuntime:
        with self._lock:
            return self._state(item_id)

    def snapshot(self) -> List[Dict[str, Any]]:
        """返回所有主播的展示数据（含排队/闸门/统计字段，P1 新增）。"""
        stats = self.stats()
        rows: List[Dict[str, Any]] = []
        items = self.watchlist.all()
        with self._lock:
            for item in items:
                rt = self._state(item.id)
                row = rt.to_dict()
                queued_info = self._queue.get(item.id)
                user_quality = self._user_quality(item)
                effective_quality = self._session_quality.get(item.id) or user_quality
                degraded = effective_quality != user_quality
                estimated = self._recorder_mbps.get(item.id)
                if estimated is None and queued_info:
                    estimated = queued_info.get("mbps")
                if estimated is None:
                    estimated = estimate_mbps(effective_quality)
                row.update({
                    "id": item.id,
                    "name": item.display_name,
                    "raw_name": item.name,
                    "room": item.web_rid or item.target or item.url,
                    "web_rid": item.web_rid,
                    "target": item.target,
                    "stream_url": self._stream_urls.get(item.id, ""),
                    "last_live_at": int(getattr(item, "last_live_at", 0) or 0),
                    "homepage": self._item_homepage(item),
                    "live_url": (("https://live.douyin.com/%s" % item.web_rid)
                                 if item.web_rid else ""),
                    "auto_record": item.auto_record,
                    "quality": item.quality,
                    # ---- 清晰度降档（P1-E，只增不删）----
                    "effective_quality": effective_quality,
                    "degraded": degraded,
                    "enabled": item.enabled,
                    "note": item.note,
                    "group": getattr(item, "group", ""),
                    # ---- 排队 / 闸门（P1）----
                    "queued": bool(queued_info),
                    "queue_reason": (queued_info or {}).get("reason", ""),
                    "estimated_mbps": round(float(estimated), 2),
                    # ---- 全局统计（P1；每行携带一份，GUI 可直接取）----
                    "rec_count": stats["rec_count"],
                    "queued_count": stats["queued_count"],
                    "requests": stats["requests"],
                    "success_rate": stats["success_rate"],
                    "avg_latency_ms": stats["avg_latency_ms"],
                    "risk_count": stats["risk_count"],
                    "breaker_state": stats["breaker_state"],
                })
                # 同步到 RoomRuntime 动态属性（不改 model.py；GUI getattr 也可读）
                try:
                    setattr(rt, "effective_quality", effective_quality)
                    setattr(rt, "degraded", degraded)
                except Exception:  # pragma: no cover
                    pass
                rows.append(row)
        return rows

    @staticmethod
    def _item_homepage(item) -> str:
        """推导抖音主页链接（用于右键「跳转到抖音主页」）。

        只认「主播主页」：优先 item.sec_uid，其次 url/target 里的 /user/<sec_uid>；
        绝不返回直播间地址——否则「跳转抖音主页」会跳到直播间。
        """
        import re as _re
        sec = str(getattr(item, "sec_uid", "") or "").strip()
        if sec and _re.fullmatch(r"[A-Za-z0-9_\-]+", sec):
            return "https://www.douyin.com/user/" + sec
        for raw in (getattr(item, "url", "") or "", getattr(item, "target", "") or ""):
            m = _re.search(r"douyin\.com/user/([A-Za-z0-9_\-]+)", raw or "")
            if m:
                return "https://www.douyin.com/user/" + m.group(1)
        return ""

    def preview_url(self, item_id: str) -> str:
        """返回某主播当前可播放的流地址（优先用本次录制缓存；未开录则现查一次）。"""
        with self._lock:
            url = self._stream_urls.get(item_id)
        if url:
            return url
        item = self.watchlist.get(item_id)
        if item is None:
            return ""
        try:
            st = self.checker.check(item)
        except Exception as exc:  # pragma: no cover
            self.log.debug("预览查询失败: %s", exc)
            return ""
        if not st or not st.is_live:
            return ""
        try:
            url = self.checker.choose_url(st, "origin") or ""
        except Exception:
            url = ""
        if url:
            with self._lock:
                self._stream_urls[item.id] = url
        return url

    def stats(self) -> Dict[str, Any]:
        """全局统计（requests/success_rate/avg_latency_ms/risk_count/在录/排队/熔断）。"""
        with self._lock:
            requests = int(self._poll_stats["requests"])
            success = int(self._poll_stats["success"])
            failed = int(self._poll_stats["failed"])
            latency = float(self._poll_stats["latency"])
            breaker = self._breaker.stats()
            uptime = int(max(0.0, time.time() - self._started_at)) if self._started_at else 0
            return {
                "requests": requests,
                "success": success,
                "failed": failed,
                "success_rate": round(success / requests, 4) if requests else 0.0,
                "avg_latency_ms": round(latency / requests * 1000.0, 1) if requests else 0.0,
                "risk_count": int(self._poll_stats["risk"]),
                "rec_count": len(self._recorders),
                "queued_count": len(self._queue),
                "in_flight": len(self._futures),
                "breaker_state": breaker["state"],
                "breaker_pause_remaining": breaker["pause_remaining"],
                "resolve_pending": len(self._resolve_pending),
                "poll_rate_limit": self.cfg.get_int("poll_rate_limit"),
                "max_poll_workers": self.cfg.get_int("max_poll_workers"),
                "uptime": uptime,
            }

    #: 兼容叫法：部分代码/面板用 monitor_stats()
    monitor_stats = stats

    # ==================================================================
    # 主播条目操作
    # ==================================================================
    def add_item(self, text: str, name: str = "", auto_record: bool = True,
                 quality: Optional[str] = None, output_template: str = "") -> Optional[WatchItem]:
        """从分享文案/链接/房间号添加主播（本地解析，随后进后台解析队列）。"""
        if not (text or "").strip():
            self._emit(EventType.ERROR, "添加失败：请输入直播间链接或房间号")
            return None
        item = self.watchlist.add_from_text(
            text,
            name=name,
            auto_record=auto_record,
            quality=quality or self.cfg.get_str("quality"),
            output_template=output_template,
            save=False,
        )
        self._schedule_watchlist_save()
        self._emit(EventType.INFO, "已添加主播：%s" % item.display_name, item=item)
        self._enqueue_resolve(item.id, text)
        return item

    def add_items(self, texts: List[str], name: str = "", auto_record: bool = True,
                  quality: Optional[str] = None,
                  output_template: str = "") -> List[WatchItem]:
        """批量添加主播（一次落盘 + 后台限速解析；GUI 批量导入请用这个）。"""
        pairs: List[Tuple[WatchItem, str]] = []
        for text in texts:
            if not (text or "").strip():
                continue
            item = self.watchlist.add_from_text(
                text,
                name=name,
                auto_record=auto_record,
                quality=quality or self.cfg.get_str("quality"),
                output_template=output_template,
                save=False,
            )
            pairs.append((item, text))
        if not pairs:
            return []
        self._schedule_watchlist_save()
        self.log.info("批量导入 %d 个主播（后台解析中）", len(pairs))
        self._emit(EventType.INFO, "批量导入 %d 个主播（后台解析中）" % len(pairs))
        for item, text in pairs:
            self._enqueue_resolve(item.id, text)
        return [item for item, _text in pairs]

    def update_item(self, item: WatchItem) -> None:
        # save=False + 合并保存：GUI 批量导入时逐条 update 也不会写出 O(n^2) 的 JSON
        self.watchlist.update(item, save=False)
        self._schedule_watchlist_save()
        with self._lock:
            self._state(item.id)
        self._emit(EventType.INFO, "已更新主播：%s" % item.display_name, item=item)

    def remove_item(self, item_id: str) -> None:
        self._stop_recording(item_id, "主播已删除")
        item = self.watchlist.get(item_id)
        if item is not None:
            self.watchlist.remove(item_id)
            self._emit(EventType.INFO, "已删除主播：%s" % item.display_name)
        with self._lock:
            self._states.pop(item_id, None)
            self._last_status.pop(item_id, None)
            self._queue.pop(item_id, None)
            self._manual_queue.discard(item_id)
            self._recorder_mbps.pop(item_id, None)
            self._last_live_at.pop(item_id, None)
            self._scheduled.pop(item_id, None)
            self._session_quality.pop(item_id, None)
            self._session_reconnects.pop(item_id, None)
            self._degrade_restart.pop(item_id, None)
            self._manual_sessions.discard(item_id)

    def resolve_text_now(self, text: str) -> Dict[str, Any]:
        """同步解析（可能联网，耗时最多十几秒，GUI 请用批量解析队列）。"""
        return self.checker.resolve(text)

    def _enqueue_resolve(self, item_id: str, text: str) -> None:
        """把「解析主播信息」放入限速队列（不再每条开一个线程）。"""
        if not (text or "").strip():
            return
        self._ensure_workers()
        with self._lock:
            if item_id in self._resolve_pending:
                return
            self._resolve_pending.add(item_id)
        self._resolve_queue.put((item_id, text, 0))

    def _resolve_async(self, item_id: str, text: str) -> None:
        """兼容旧接口：等价于入解析队列。"""
        self._enqueue_resolve(item_id, text)

    # ==================================================================
    # 优先队列调度（P1 核心）
    # ==================================================================
    def _init_scheduler(self) -> None:
        max_workers = max(1, self.cfg.get_int("max_poll_workers"))
        with self._lock:
            self._heap = []
            self._heap_seq = 0
            self._scheduled = {}
            self._in_flight.clear()
            self._futures.clear()
            now = time.monotonic()
            for item in self.watchlist.enabled():
                self._schedule_locked(item.id, now)
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="dypoll")

    def _schedule_locked(self, item_id: str, due: float) -> None:
        """入队/更新某个房间的到期时间（调用方需持锁）。"""
        self._heap_seq += 1
        heapq.heappush(self._heap, (due, self._heap_seq, item_id))
        self._scheduled[item_id] = due

    def _schedule(self, item_id: str, due: float) -> None:
        with self._lock:
            self._schedule_locked(item_id, due)

    def _pop_due_entry(self, now: float):
        """取出一个到期任务；跳过被重新调度覆盖的旧条目。返回 (item_id, due)。"""
        with self._lock:
            while self._heap:
                due, _seq, item_id = self._heap[0]
                if due > now:
                    return None
                heapq.heappop(self._heap)
                if self._scheduled.get(item_id) != due:
                    continue
                self._scheduled.pop(item_id, None)
                if item_id in self._in_flight:
                    continue
                return item_id, due
            return None

    def _next_due_at(self):
        """最近的到期时间（清理陈旧堆条目）；无任务返回 None。"""
        with self._lock:
            while self._heap:
                due, _seq, item_id = self._heap[0]
                if self._scheduled.get(item_id) != due:
                    heapq.heappop(self._heap)
                    continue
                return due
            return None

    def _poll_one(self, item: WatchItem):
        """worker：查询单个房间状态，返回 (item, StatusView, 耗时秒)。"""
        started = time.monotonic()
        try:
            status = self.checker.check(item)
        except Exception as exc:  # pragma: no cover - checker 内部已兜底
            status = StatusView(is_live=False, error="查询异常: %s" % exc)
        return item, status, time.monotonic() - started

    def _dispatch_due(self, now: float) -> None:
        """把到期房间提交到固定工作池（受令牌桶与熔断器约束）。"""
        executor = self._executor
        if executor is None or self._stop_event.is_set():
            return
        max_workers = max(1, self.cfg.get_int("max_poll_workers"))
        while len(self._futures) < max_workers:
            if self._stop_event.is_set():
                return
            if not self._breaker.allow(now):
                return
            entry = self._pop_due_entry(now)
            if entry is None:
                return
            item_id, due = entry
            if not self._poll_bucket.try_acquire(now):
                # 令牌不够：放回队列，稍后重试（不阻塞调度线程）
                self._schedule_locked(item_id, due)
                return
            item = self.watchlist.get(item_id)
            if item is None or not item.enabled:
                continue
            try:
                future = executor.submit(self._poll_one, item)
            except RuntimeError:  # pragma: no cover - 线程池已关闭
                self._schedule_locked(item_id, time.monotonic() + 1.0)
                return
            with self._lock:
                self._in_flight.add(item_id)
                self._futures[future] = (item, time.monotonic())
                self._schedule_locked(item_id, time.monotonic() + 3600.0)  # 占位，结果回来再定档

    def _collect_futures(self, now: float) -> None:
        """收集已完成的查询并更新状态。"""
        if not self._futures:
            return
        futures = list(self._futures.keys())
        done, _pending = wait(futures, timeout=0)
        for future in done:
            with self._lock:
                item, started = self._futures.pop(future, (None, now))
                if item is not None:
                    self._in_flight.discard(item.id)
            latency = max(0.0, time.monotonic() - (started or now))
            try:
                item, status, latency = future.result()
            except Exception as exc:  # pragma: no cover
                status = StatusView(is_live=False, error="查询异常: %s" % exc)
            self._record_poll_result(status, latency)
            if item is not None and not self._stop_event.is_set():
                self._apply_check(item, status)

    def _record_poll_result(self, status: StatusView, latency: float) -> None:
        """记录一次查询结果：统计 + 熔断判定（触发时刷新 ttwid）。"""
        ok = not bool(status.error)
        risk = (not ok) and _looks_like_risk(status.error)
        with self._lock:
            self._poll_stats["requests"] += 1
            self._poll_stats["latency"] += float(latency or 0)
            if ok:
                self._poll_stats["success"] += 1
            else:
                self._poll_stats["failed"] += 1
                if risk:
                    self._poll_stats["risk"] += 1
            was_open = self._breaker.state == CircuitBreaker.OPEN
        self._breaker.record(ok, latency, risk=risk)
        if self._breaker.state == CircuitBreaker.OPEN and not was_open:
            pause = self._breaker.pause_seconds
            message = ("查询错误率连续 2 个窗口超过 %.0f%%，触发熔断："
                       "暂停 %.0f 秒并刷新会话" % (self._breaker.error_rate * 100, pause))
            self.log.warning(message)
            self._emit(EventType.INFO, message)
            self._refresh_cookie()

    def _refresh_cookie(self) -> bool:
        """熔断时强制刷新抖音会话（dy_api.get_cookie(force=True)）。"""
        api = getattr(self.checker, "api", None)
        getter = getattr(api, "get_cookie", None)
        if not callable(getter):
            return False
        try:
            try:
                cookie = getter(force=True)
            except TypeError:  # 老版本/自定义协议层没有 force 参数
                cookie = getter()
            count = len(cookie) if isinstance(cookie, dict) else 1
            self.log.info("已强制刷新抖音会话 cookie（%d 项）", count)
            return bool(cookie)
        except Exception as exc:
            self.log.warning("强制刷新 cookie 失败: %s", exc)
            return False

    def _loop(self) -> None:
        """调度主循环：派发到期任务 -> 收集结果 -> 闸门/录制管理。"""
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                self._collect_futures(now)
                self._dispatch_due(now)
                self._maybe_cleanup_states(time.time())
                if now - self._gate_last_at >= 1.0:
                    self._gate_last_at = now
                    self._reap_recorders()
                    self._enforce_stop()
                    self._check_disk_watchdog()
                    if not self._stop_event.is_set():
                        self._start_pending()
                    if self._pending_cleanup and not self._stop_event.is_set():
                        self._pending_cleanup = False
                        self._cleanup_disk()
                timeout = 0.05 if self._futures else self._idle_timeout(now)
                if timeout > 0:
                    self._wake_event.wait(timeout)
                    self._wake_event.clear()
        except Exception:  # pragma: no cover - 兜底，监控不能停
            self.log.exception("监控调度循环异常")
        finally:
            self._shutdown_scheduler()
            with self._lock:
                recorders = list(self._recorders.values())
            for rec in recorders:
                rec.stop("监控退出")
            for rec in recorders:
                rec.join(30)
            with self._lock:
                self._recorders.clear()
                self._futures.clear()
                self._in_flight.clear()
            self.log.info("监控线程结束")

    def _idle_timeout(self, now: float) -> float:
        """空闲等待时长：最多 1s，且被最近到期任务/熔断恢复时间约束。"""
        timeout = 1.0
        due = self._next_due_at()
        if due is not None:
            timeout = min(timeout, max(0.02, due - now))
        paused = self._breaker.paused_until()
        if paused:
            timeout = min(timeout, max(0.02, paused - now))
        if self._breaker.state == CircuitBreaker.HALF_OPEN:
            timeout = min(timeout, 0.1)
        return max(0.02, timeout)

    def _maybe_cleanup_states(self, now_wall: float) -> None:
        """定期清理已删除条目的状态，避免长期挂机内存增长。"""
        if now_wall - self._last_cleanup_at < 10.0:
            return
        self._last_cleanup_at = now_wall
        valid_ids = {item.id for item in self.watchlist.all()}
        with self._lock:
            for item_id in list(self._states.keys()):
                if item_id not in valid_ids:
                    self._states.pop(item_id, None)
                    self._last_status.pop(item_id, None)
                    self._queue.pop(item_id, None)
                    self._manual_queue.discard(item_id)
                    self._recorder_mbps.pop(item_id, None)
                    self._last_live_at.pop(item_id, None)
                    self._session_quality.pop(item_id, None)
                    self._session_reconnects.pop(item_id, None)
                    self._degrade_restart.pop(item_id, None)
                    self._manual_sessions.discard(item_id)

    def poll_interval_for(self, item_id: str = "", is_live: bool = False,
                          failures: int = 0, now: Optional[float] = None) -> float:
        """计算某房间下一次查询间隔（秒）：自适应档位 + 失败指数退避。"""
        now = time.time() if now is None else float(now)
        live = max(1, self.cfg.get_int("poll_interval_live"))
        warm = max(1, self.cfg.get_int("poll_interval_warm"))
        cold = max(1, self.cfg.get_int("poll_interval_cold"))
        if is_live:
            base = float(live)
        else:
            last_live = self._last_live_at.get(item_id, 0.0)
            if last_live and (now - last_live) <= WARM_WINDOW_SECONDS:
                base = float(warm)
            else:
                base = float(cold)
        if failures and failures > 0:
            # 失败退避独立于档位：30s -> 60s -> 120s -> ... -> 封顶 300s
            start = max(1, self.cfg.get_int("poll_backoff_start"))
            cap = max(start, self.cfg.get_int("poll_backoff_max"))
            return float(min(cap, start * (2 ** min(int(failures) - 1, 10))))
        return base

    @staticmethod
    def _state_key(rt: RoomRuntime) -> str:
        if rt.recording:
            return "recording"
        if rt.error:
            return "error"
        if rt.is_live:
            return "live"
        return "offline"

    def _log_state_change(self, item: WatchItem, old_key: str, new_key: str,
                          status: StatusView) -> None:
        """只有状态变化才打 INFO 日志（1000+ 房间时避免日志刷屏）。"""
        if new_key == "error":
            self.log.warning("[%s] 查询失败：%s", item.display_name,
                             str(status.error or "")[:200])
        elif new_key == "live" and old_key != "live":
            self.log.info("[%s] 开播：%s", item.display_name, status.title or "")
        elif new_key == "offline" and old_key in ("live", "recording"):
            self.log.info("[%s] 下播/未开播", item.display_name)
        elif new_key == "offline" and old_key == "error":
            self.log.info("[%s] 查询恢复（当前未开播）", item.display_name)
        else:
            self.log.debug("[%s] 状态: %s -> %s", item.display_name, old_key, new_key)

    def _apply_check(self, item: WatchItem, st: StatusView) -> None:
        """应用一次查询结果：更新运行状态、自适应排下一次查询、状态变化日志。"""
        now_wall = time.time()
        with self._lock:
            rt = self._state(item.id)
            old_key = self._state_key(rt)
            rt.last_check_at = now_wall
            rt.updated_at = now_wall
            if st.error:
                kind = classify_lookup_error(st.error)
                if kind == "offline":
                    # 账号当前没有直播间（未开播/无直播数据）→ 按离线处理，不显示成错误
                    rt.consecutive_failures = 0
                    rt.error = ""
                    rt.is_live = False
                    if not rt.recording:
                        rt.status = STATUS_OFFLINE
                    self._queue.pop(item.id, None)
                    self._manual_queue.discard(item.id)
                    self._stream_urls.pop(item.id, None)
                else:
                    rt.consecutive_failures += 1
                    rt.error = describe_lookup_error(st.error)
                    if not rt.recording:
                        rt.status = STATUS_ERROR
                    self._queue.pop(item.id, None)
                    self._manual_queue.discard(item.id)
                    # 条目本身无效（只有昵称/房间号错/链接错）→ 连续 N 次后自动停用，不再反复查询
                    if kind == "invalid":
                        try:
                            limit = max(0, self.cfg.get_int("invalid_auto_disable_after"))
                        except Exception:
                            limit = 3
                        if limit and rt.consecutive_failures >= limit and item.enabled:
                            item.enabled = False
                            rt.error = rt.error + "（已自动停用，右键可重新启用）"
                            disabled_now = True
                            self.log.warning("[%s] 条目无效，已自动停用：%s",
                                             item.display_name, st.error)
                            self._emit(EventType.INFO,
                                       "条目无效，已自动停用：%s" % item.display_name,
                                       item=item)
            else:
                rt.consecutive_failures = 0
                rt.error = ""
                rt.is_live = st.is_live
                rt.title = st.title or rt.title
                rt.anchor_name = st.anchor_name or rt.anchor_name
                if st.is_live:
                    self._last_live_at[item.id] = now_wall
                    try:
                        prev_live = int(getattr(item, "last_live_at", 0) or 0)
                        if now_wall - prev_live >= 300:
                            item.last_live_at = int(now_wall)
                            live_updated = True
                        elif not prev_live:
                            item.last_live_at = int(now_wall)
                            live_updated = True
                    except Exception:
                        pass
                else:
                    self._queue.pop(item.id, None)
                    self._manual_queue.discard(item.id)
                    self._stream_urls.pop(item.id, None)
                    # 下播：本场降档状态失效，下次开播恢复用户设定档位
                    self._session_quality.pop(item.id, None)
                    self._session_reconnects.pop(item.id, None)
                    self._degrade_restart.pop(item.id, None)
                    try:
                        setattr(rt, "effective_quality", self._user_quality(item))
                        setattr(rt, "degraded", False)
                    except Exception:  # pragma: no cover
                        pass
                self._last_status[item.id] = st
                if rt.recording:
                    rt.status = STATUS_RECORDING
                elif st.is_live:
                    # 开播但被闸门拦下时保持「排队中」，不要被轮询结果覆盖
                    rt.status = STATUS_QUEUED if item.id in self._queue else STATUS_LIVE
                else:
                    rt.status = STATUS_OFFLINE
            interval = self.poll_interval_for(item.id, rt.is_live,
                                              rt.consecutive_failures, now=now_wall)
            rt.next_check_at = now_wall + interval
            new_key = self._state_key(rt)
            self._schedule_locked(item.id, time.monotonic() + interval)

        if old_key != new_key:
            self._log_state_change(item, old_key, new_key, st)

        # 主播名/房间号补全写回 watchlist（仅当当前为空；批量合并保存）
        need_save = (bool(locals().get("live_updated", False))
                     or bool(locals().get("disabled_now", False)))
        if st.anchor_name and not item.name:
            item.name = st.anchor_name
            need_save = True
        if st.room_id and not item.web_rid:
            item.web_rid = st.room_id
            need_save = True
        if not getattr(item, "sec_uid", ""):
            sec = _status_sec_uid(st)
            if sec:
                item.sec_uid = sec
                need_save = True
        if need_save:
            self.watchlist.update(item, save=False)
            self._schedule_watchlist_save()

    def _cached_live(self, item_id: str) -> Optional[bool]:
        """供录制引擎判断是否下播（读缓存，不联网）。"""
        with self._lock:
            rt = self._states.get(item_id)
            if rt is None:
                return None
            if rt.error:
                return None
            return rt.is_live

    # ==================================================================
    # 清晰度（默认最高档 + 断流自动降档，P1-E）
    # ==================================================================
    def _user_quality(self, item: WatchItem) -> str:
        """用户设定的目标清晰度（item.quality 优先，空则用全局 quality）。

        全局 quality="best" 作为「默认最高档」：房间条目仍是默认 origin（等价于未指定档位）
        时使用 best（透传给 checker.choose_url），房间显式 hd/sd 时仍以房间设置为准。
        """
        item_quality = str(getattr(item, "quality", "") or "").strip().lower()
        global_quality = self.cfg.get_str("quality").strip().lower() or "origin"
        if global_quality not in ("origin", "best", "hd", "sd"):
            global_quality = "origin"
        if global_quality == "best" and item_quality in ("", "origin"):
            return "best"
        quality = item_quality or global_quality
        if quality not in ("origin", "best", "hd", "sd"):
            quality = "origin"
        return quality

    def _ladder_qualities(self) -> List[str]:
        """降档阶梯（从左到右由高到低）。"""
        result: List[str] = []
        raw = self.cfg.get_str("quality_ladder") or "origin,hd,sd"
        for part in raw.replace("，", ",").split(","):
            value = part.strip().lower()
            if value in ("origin", "best", "hd", "sd") and value not in result:
                result.append(value)
        return result or ["origin", "hd", "sd"]

    def _next_degrade_quality(self, current: str) -> Optional[str]:
        """返回当前档位在阶梯里的下一档；已是最低档/无法降档返回 None。"""
        ladder = self._ladder_qualities()
        key = (current or "").strip().lower()
        if key in ladder:
            index = ladder.index(key)
        elif key == "best" and "origin" in ladder:
            # best 视为最高档：从 origin 的下一档开始降
            index = ladder.index("origin")
        else:
            index = -1
        if index >= 0 and index + 1 < len(ladder):
            target = ladder[index + 1]
            return target if target != key else None
        return None

    def _clear_session_quality(self, item_id: str,
                               item: Optional[WatchItem] = None) -> None:
        """结束本场录制：清空降档状态，下次开播恢复用户设定档位。"""
        with self._lock:
            self._session_quality.pop(item_id, None)
            self._session_reconnects.pop(item_id, None)
            self._degrade_restart.pop(item_id, None)
            self._manual_sessions.discard(item_id)
            rt = self._states.get(item_id)
        if rt is not None:
            try:
                setattr(rt, "effective_quality",
                        self._user_quality(item) if item is not None else "")
                setattr(rt, "degraded", False)
            except Exception:  # pragma: no cover
                pass

    def _maybe_note_reconnect_locked(self, item_id: str, rt: RoomRuntime):
        """累计本场重连次数；达到阈值且可降档时返回降档动作（调用方需持锁）。"""
        if item_id in self._degrade_restart:
            return None
        count = self._session_reconnects.get(item_id, 0) + 1
        self._session_reconnects[item_id] = count
        if not self.cfg.get_bool("quality_degrade_enabled"):
            return None
        threshold = max(1, self.cfg.get_int("quality_degrade_after_reconnects"))
        if count < threshold:
            return None
        item = self.watchlist.get(item_id)
        if item is None:
            return None
        current = self._session_quality.get(item_id) or self._user_quality(item)
        target = self._next_degrade_quality(current)
        if not target:
            return None
        # 立即把本场有效档位切到目标档，重连计数归零，并标记「降档后重开」
        self._session_quality[item_id] = target
        self._session_reconnects[item_id] = 0
        self._degrade_restart[item_id] = target
        try:
            setattr(rt, "effective_quality", target)
            setattr(rt, "degraded", target != self._user_quality(item))
        except Exception:  # pragma: no cover
            pass
        return item, count, current, target

    def _perform_degrade(self, item: WatchItem, count: int,
                         old_quality: str, new_quality: str) -> None:
        """执行降档：写 INFO 事件/日志/历史，并停止当前 recorder 用新档位重开。"""
        message = "连续断流 %d 次，本次清晰度降为 %s" % (count, new_quality)
        self.log.info("[%s] %s（%s -> %s）", item.display_name,
                      message, old_quality, new_quality)
        self._emit(EventType.INFO, message, item=item)
        try:
            # history 没有独立的 events 表：降档诊断信息写入 errors 表，便于诊断包分析
            historymod.record_error(
                "manager:quality",
                "[%s] %s（%s -> %s）" % (item.display_name, message,
                                        old_quality, new_quality),
            )
        except Exception:  # pragma: no cover
            self.log.debug("写清晰度降档历史失败", exc_info=True)
        with self._lock:
            rec = self._recorders.get(item.id)
            rt = self._states.get(item.id)
            if rt is not None:
                rt.reconnecting = False
                rt.status = STATUS_LIVE if rt.is_live else rt.status
        if rec is not None:
            rec.stop("断流降档：%s -> %s" % (old_quality, new_quality))

    # ==================================================================
    # 并发闸门 + 录制启停
    # ==================================================================
    def _reap_recorders(self) -> None:
        finished: List[str] = []
        with self._lock:
            for item_id, rec in list(self._recorders.items()):
                if not rec.is_alive():
                    finished.append(item_id)
            for item_id in finished:
                rec = self._recorders.pop(item_id, None)
                self._recorder_mbps.pop(item_id, None)
                rt = self._states.get(item_id)
                item = self.watchlist.get(item_id)
                restart_quality = self._degrade_restart.pop(item_id, None)
                if rt is not None and rec is not None:
                    stt = rec.status()
                    rt.recording = False
                    rt.reconnecting = False
                    rt.duration_sec = stt["duration"]
                    rt.file_size = stt["file_size"]
                    rt.output_path = stt["current_file"] or rt.output_path
                    rt.segment_count = stt["file_count"]
                    if rt.status in (STATUS_RECORDING, STATUS_RECONNECTING, STATUS_QUEUED):
                        rt.status = STATUS_LIVE if (rt.is_live and not rt.error) else STATUS_OFFLINE
                if restart_quality is not None:
                    # 断流降档后重开：本场继续，保留新的有效档位
                    self._session_quality[item_id] = restart_quality
                    try:
                        if rt is not None:
                            setattr(rt, "effective_quality", restart_quality)
                            setattr(rt, "degraded",
                                    restart_quality != (self._user_quality(item) if item else restart_quality))
                    except Exception:  # pragma: no cover
                        pass
                else:
                    # 正常结束/手动停止/下播：本场结束，清空降档状态（下次开播恢复用户设定）
                    self._clear_session_quality(item_id, item)
        if finished:
            self._pending_cleanup = True
            self.log.info("录制结束并回收: %s", finished)

    def _enforce_stop(self) -> None:
        with self._lock:
            entries = list(self._recorders.items())
        for item_id, rec in entries:
            item = self.watchlist.get(item_id)
            rt = self._states.get(item_id)
            if item is None or not item.enabled:
                self._stop_recording(item_id, "主播已删除或已禁用")
                continue
            if rt is not None and not rt.error and not rt.is_live:
                self._stop_recording(item_id, "直播已结束（下播）")

    def _stop_recording(self, item_id: str, reason: str) -> None:
        with self._lock:
            rec = self._recorders.get(item_id)
            self._queue.pop(item_id, None)
            self._manual_queue.discard(item_id)
            self._recorder_mbps.pop(item_id, None)
        self._clear_session_quality(item_id, self.watchlist.get(item_id))
        if rec is None:
            return
        self.log.info("停止录制 [%s]: %s", item_id, reason)
        rec.stop(reason)

    def _disk_status(self):
        """返回 (剩余GB, 保留GB)。"""
        try:
            free = float(diskmod.free_gb(self.cfg.output_root))
        except Exception:
            free = 0.0
        try:
            reserve = float(self.cfg.get_float("min_free_gb"))
        except Exception:
            reserve = 0.0
        return free, max(0.0, reserve)

    def protected_files(self) -> List[Path]:
        """正在录制/写入的文件路径（清理输出目录时用于保护，绝不删除）。"""
        out: List[Path] = []
        with self._lock:
            recorders = list(self._recorders.values())
        for rec in recorders:
            try:
                st = rec.status()
            except Exception:
                continue
            for key in ("files", "current_file"):
                val = st.get(key)
                if isinstance(val, (list, tuple)):
                    for p in val:
                        if p:
                            out.append(Path(p))
                elif val:
                    out.append(Path(val))
        return out

    def apply_disk_policy(self, reason: str = "设置已更新") -> Dict[str, Any]:
        """立即应用「最小剩余空间」设置。

        更新所有在录任务的阈值；若剩余空间已低于保留值，立即停止全部录制并暂停开录。
        返回 {blocked, free, reserve, stopped, message}。
        """
        free, reserve = self._disk_status()
        with self._lock:
            recorders = list(self._recorders.values())
        for rec in recorders:
            try:
                rec.set_min_free_gb(reserve)
            except Exception:
                pass
        result: Dict[str, Any] = {"blocked": False, "free": free,
                                  "reserve": reserve, "stopped": 0, "message": ""}
        if reserve and free < reserve:
            self._disk_blocked = True
            stopped = 0
            for rec in recorders:
                try:
                    rec.stop("磁盘空间不足（剩余 %.1f GB < 保留 %.1f GB）"
                             % (free, reserve))
                    stopped += 1
                except Exception:
                    pass
            with self._lock:
                self._queue.clear()
            try:
                self._manual_queue.clear()
            except Exception:
                pass
            msg = ("磁盘剩余 %.1f GB 低于保留空间 %.1f GB：已停止 %d 路录制并暂停开录。"
                   "请清理磁盘或调低保留值。" % (free, reserve, stopped))
            self.log.warning("磁盘策略（%s）：%s", reason, msg)
            self._emit(EventType.DISK, msg)
            result.update({"blocked": True, "stopped": stopped, "message": msg})
        else:
            # 恢复前加滞回：剩余空间要明显高于保留值才继续，避免在阈值附近反复停/开
            recover_at = reserve + max(2.0, reserve * 0.25)
            if self._disk_blocked and free < recover_at:
                result.update({
                    "blocked": True, "stopped": 0,
                    "message": "磁盘仍在恢复中：剩余 %.1f GB，需达到 %.1f GB 才继续录制"
                               % (free, recover_at),
                })
                return result
            if self._disk_blocked:
                msg = ("磁盘空间已恢复：剩余 %.1f GB（保留 %.1f GB），继续监控/录制"
                       % (free, reserve))
                self.log.info(msg)
                self._emit(EventType.INFO, msg)
            self._disk_blocked = False
        return result

    def _auto_correct_reserve(self) -> float:
        """磁盘保留值过高时自动下调（>10GB 且 >= 磁盘总容量 5%），避免录满后长期停录。

        每次 watchdog 都会调用（廉价）；只在真正需要下调时写一次配置。
        """
        try:
            reserve = float(self.cfg.get_float("min_free_gb"))
        except Exception:
            return 0.0
        if reserve <= 10.0:
            return max(0.0, reserve)
        try:
            total = diskmod.total_gb(self.cfg.output_root)
        except Exception:
            total = -1.0
        if total > 0 and reserve < total * 0.05:
            return reserve  # 大磁盘且比例小，尊重用户设置
        new_reserve = round(max(2.0, min(10.0, total * 0.02)), 1) if total > 0 else 3.0
        self.log.warning("磁盘保留值 %.1f GB 过高（磁盘总容量 %.1f GB），自动下调为 %.1f GB，"
                         "避免录满后长时间停止录制",
                         reserve, total if total > 0 else 0.0, new_reserve)
        self.cfg["min_free_gb"] = new_reserve
        try:
            self.cfg.save()
        except Exception:
            pass
        with self._lock:
            recorders = list(self._recorders.values())
        for rec in recorders:
            try:
                rec.set_min_free_gb(new_reserve)
            except Exception:
                pass
        return new_reserve

    def _check_disk_watchdog(self) -> None:
        """每秒检查磁盘：低于保留值立即停录并暂停开录；恢复后自动继续。"""
        reserve = self._auto_correct_reserve()
        free, _res = self._disk_status()
        if reserve and free < reserve:
            if not self._disk_blocked:
                self.apply_disk_policy("磁盘 watchdog")
        elif self._disk_blocked:
            self.apply_disk_policy("磁盘 watchdog")

    def _gate_block_reason(self, active: int, used_mbps: float, estimated: float) -> str:
        """返回阻塞开录的原因（空字符串 = 可以开录）。"""
        if self._disk_blocked:
            return "磁盘空间不足（已暂停开录）"
        max_concurrent = self.cfg.get_int("max_concurrent")
        if max_concurrent > 0 and active >= max_concurrent:
            return "并发上限 %d 路" % max_concurrent
        max_processes = self.cfg.get_int("max_ffmpeg_processes")
        if max_processes > 0 and active >= max_processes:
            return "ffmpeg 进程上限 %d" % max_processes
        max_mbps = self.cfg.get_int("max_total_mbps")
        if max_mbps > 0 and (used_mbps + estimated) > max_mbps + 1e-6:
            return "带宽预算 %.1f/%.0f Mbps" % (used_mbps + estimated, max_mbps)
        if not diskmod.has_enough_space(self.cfg.output_root, self.cfg.get_float("min_free_gb")):
            self._pending_cleanup = True
            return "磁盘剩余空间不足"
        return ""

    def _set_queued(self, item: WatchItem, reason: str, estimated: float) -> None:
        """标记为排队中（status=排队中，queued=True）。"""
        with self._lock:
            rt = self._state(item.id)
            if rt.recording:
                return
            previous = self._queue.get(item.id)
            if previous is None or previous.get("reason") != reason:
                self.log.debug("[%s] 进入排队：%s", item.display_name, reason)
            self._queue[item.id] = {
                "reason": reason,
                "mbps": float(estimated),
                "since": (previous or {}).get("since") or time.time(),
            }
            rt.status = STATUS_QUEUED
            rt.updated_at = time.time()

    def _clear_queue(self, item_id: str) -> None:
        with self._lock:
            self._queue.pop(item_id, None)
            self._manual_queue.discard(item_id)

    def _start_pending(self) -> None:
        """按闸门决定哪些直播间开录；超限的标记排队，闸门释放后自动开录。"""
        if self._stop_event.is_set():
            return
        candidates: List[Tuple[WatchItem, StatusView, float, str]] = []
        with self._lock:
            active = len(self._recorders)
            used_mbps = sum(self._recorder_mbps.values())
            valid_ids: Set[str] = set()
            for item in self.watchlist.enabled():
                valid_ids.add(item.id)
                rt = self._state(item.id)
                if item.id in self._recorders:
                    self._queue.pop(item.id, None)
                    self._manual_queue.discard(item.id)
                    continue
                if not (rt.is_live and not rt.error):
                    self._queue.pop(item.id, None)
                    self._manual_queue.discard(item.id)
                    # 未开播/查询失败：本场结束，恢复用户设定清晰度
                    self._session_quality.pop(item.id, None)
                    self._session_reconnects.pop(item.id, None)
                    self._degrade_restart.pop(item.id, None)
                    self._manual_sessions.discard(item.id)
                    continue
                status = self._last_status.get(item.id)
                if status is None:
                    continue
                if not (item.auto_record or item.id in self._manual_queue
                        or item.id in self._manual_sessions):
                    continue
                # 本场降档后的有效档位优先；否则用户设定档位
                quality = self._session_quality.get(item.id) or self._user_quality(item)
                estimated = estimate_mbps(quality)
                candidates.append((item, status, estimated, quality))
            for item_id in list(self._queue.keys()):
                if item_id not in valid_ids:
                    self._queue.pop(item_id, None)
                    self._manual_queue.discard(item_id)
        started = 0
        for item, status, estimated, quality in candidates:
            if self._stop_event.is_set():
                return
            reason = self._gate_block_reason(active, used_mbps, estimated)
            if reason:
                self._set_queued(item, reason, estimated)
                continue
            if self._start_recording(item, status, quality=quality):
                active += 1
                used_mbps += estimated
                started += 1
            else:
                self._set_queued(item, "启动失败", estimated)
        if started:
            self.log.info("本批开录 %d 个，当前在录 %d 个，排队 %d 个",
                          started, active, len(self._queue))

    def _on_recorder_critical(self, item_id: str, reason: str) -> None:
        """录制端报告致命磁盘问题：立即停止全部录制并暂停开录。"""
        try:
            self.log.error("录制端磁盘告警（%s）：%s", item_id, reason)
            self._disk_blocked = True
            with self._lock:
                recorders = list(self._recorders.values())
            for rec in recorders:
                try:
                    rec.stop(reason)
                except Exception:
                    pass
            self._emit(EventType.DISK,
                       "磁盘空间不足，已停止全部录制：%s" % reason)
        except Exception:  # pragma: no cover
            self.log.exception("处理录制端磁盘告警失败")

    def _make_stream_url_provider(self, item_id: str, quality: str, initial_url: str):
        """返回一个「每次重连都重新取流地址」的回调，供录制器使用。

        抖音拉流地址带签名、有时效，重连必须重新取，否则会用过期地址无限空转。
        - 首次直接返回 initial_url（避免重复查询）；
        - 之后每次重连都现查一次，取到新地址就缓存；查询失败/已下播时回退上次地址；
        - 加 15 秒节流，避免查询风暴。
        """
        state = {"ts": time.time(), "url": initial_url}

        def provider() -> Optional[str]:
            now = time.time()
            if state["url"] and now - state["ts"] < 15.0:
                return state["url"]
            item = self.watchlist.get(item_id)
            if item is None:
                return state["url"] or None
            # 复用查询限速令牌，避免大量重连同时现查把接口打爆；拿不到令牌就回退旧地址
            try:
                if not self._poll_bucket.try_acquire(time.monotonic()):
                    return state["url"] or None
            except Exception:
                pass
            try:
                st = self.checker.check(item)
            except Exception:
                st = None
            if st is not None and getattr(st, "is_live", False) and not getattr(st, "error", ""):
                url = self.checker.choose_url(st, quality)
                if url:
                    with self._lock:
                        self._stream_urls[item_id] = url
                        self._last_status[item_id] = st
                    state["ts"] = now
                    state["url"] = url
                    return url
            with self._lock:
                fallback = self._stream_urls.get(item_id, "") or state["url"]
            return fallback or None

        return provider

    def _start_recording(self, item: WatchItem, st: StatusView,
                         quality: Optional[str] = None) -> bool:
        quality = str(quality or self._session_quality.get(item.id)
                      or self._user_quality(item)).strip().lower()
        if quality not in ("origin", "best", "hd", "sd"):
            quality = "origin"
        # quality="best" 直接透传给 checker.choose_url（dy_api 按 qualities 元数据取最高可用档）
        url = self.checker.choose_url(st, quality)
        if not url:
            with self._lock:
                rt = self._state(item.id)
                rt.error = "未获取到可录制的流地址"
                rt.status = STATUS_ERROR
            self._emit(EventType.ERROR, "未获取到流地址：%s" % item.display_name, item=item)
            return False
        with self._lock:
            self._stream_urls[item.id] = url
        # 录制器每次重连都会调用 provider 重新取流地址，避免复用过期签名地址
        provider = self._make_stream_url_provider(item.id, quality, url)

        try:
            out_dir = render_output_dir(
                item,
                self.cfg.get_str("output_template"),
                self.cfg.output_root,
                title=st.title,
            )
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log.error("创建输出目录失败: %s", exc)
            self._emit(EventType.ERROR, "创建输出目录失败: %s" % exc, item=item)
            return False

        if not diskmod.has_enough_space(out_dir, self.cfg.get_float("min_free_gb")):
            self._pending_cleanup = True
            self._emit(
                EventType.DISK,
                "磁盘剩余空间不足（%.1f GB），暂不开始录制 %s"
                % (diskmod.free_gb(out_dir), item.display_name),
                item=item,
            )
            return False

        base = render_filename(item, self.cfg.get_str("filename_template"), title=st.title)
        base = self._unique_base(out_dir, base)
        try:
            ffmpeg_path = self._ffmpeg or ffmpeg_util.resolve_ffmpeg(self.cfg.get_str("ffmpeg_path"))[0]
            rec = RoomRecorder(
                ffmpeg_path,
                provider,
                out_dir,
                base,
                name=item.display_name,
                room_id=item.room_key,
                quality=quality,
                min_keep_seconds=self.cfg.get_int("min_keep_seconds"),
                container=self.cfg.get_str("container"),
                remux_mp4=self.cfg.get_bool("remux_mp4"),
                segment_remux_now=self.cfg.get_bool("segment_remux_now"),
                keep_source=self.cfg.get_bool("keep_source"),
                segment_duration=self.cfg.get_int("segment_duration"),
                segment_size_mb=self.cfg.get_int("segment_size_mb"),
                retry_max=self.cfg.get_int("retry_max"),
                retry_interval=self.cfg.get_int("retry_interval"),
                retry_interval_max=self.cfg.get_int("retry_interval_max"),
                stall_check_seconds=self.cfg.get_int("stall_check_seconds"),
                stall_kill_after=self.cfg.get_int("stall_kill_after"),
                min_free_gb=self.cfg.get_float("min_free_gb"),
                graceful_stop_timeout=self.cfg.get_float("graceful_stop_timeout"),
                record_timeout_min=self.cfg.get_int("record_timeout_min"),
                extra_args=self.cfg.get_list("ffmpeg_extra_args"),
                http_input_args=self.cfg.get_list("ffmpeg_http_args"),
                # 注意：recorder.py 的 is_live_fn 契约历史上有歧义（监视循环把 False 当离线，
                # ffmpeg 自行退出分支却把 True 当离线），传进去会把「断流」误判成「下播」。
                # 因此这里不传 is_live_fn：下播统一由 manager 轮询 + _enforce_stop 判定，
                # 断流交给 recorder 正常重连（产生 RECONNECT 事件，供清晰度自动降档使用）。
                is_live_fn=None,
                on_event=lambda ev, iid=item.id: self._on_recorder_event(ev, iid),
                on_critical=lambda reason, iid=item.id: self._on_recorder_critical(iid, reason),
            )
        except Exception as exc:  # pragma: no cover
            self.log.exception("创建录制任务失败")
            self._emit(EventType.ERROR, "创建录制任务失败: %s" % exc, item=item)
            return False
        with self._lock:
            self._recorders[item.id] = rec
            self._recorder_mbps[item.id] = estimate_mbps(quality)
            self._queue.pop(item.id, None)
            self._manual_queue.discard(item.id)
            # 非自动录制 = 手动会话：本场降档重开也要继续（不依赖 auto_record）
            if item.auto_record:
                self._manual_sessions.discard(item.id)
            else:
                self._manual_sessions.add(item.id)
            rt = self._state(item.id)
            rt.recording = True
            rt.reconnecting = False
            rt.record_started_at = time.time()
            rt.status = STATUS_RECORDING
            rt.error = ""
            rt.stop_reason = ""
            rt.output_path = str(out_dir / base)
            self._session_quality[item.id] = quality
            try:
                setattr(rt, "effective_quality", quality)
                setattr(rt, "degraded", quality != self._user_quality(item))
            except Exception:  # pragma: no cover
                pass
        rec.start()
        self.log.info("开始录制 %s（清晰度 %s）-> %s",
                      item.display_name, quality, out_dir / base)
        return True

    def _unique_base(self, out_dir: Path, base: str) -> str:
        """避免同名文件覆盖：已存在则追加 _2/_3...。"""
        candidate = base
        idx = 2
        while True:
            exists = any(
                p.name.startswith(candidate) and p.suffix.lower() in (".flv", ".mp4")
                for p in out_dir.iterdir()
            ) if out_dir.exists() else False
            if not exists:
                return candidate
            candidate = "%s_%d" % (base, idx)
            idx += 1

    # ==================================================================
    # 事件与清理
    # ==================================================================
    def _on_recorder_event(self, ev: RecordEvent, item_id: str) -> None:
        degrade_action = None
        with self._lock:
            rt = self._states.get(item_id)
            if rt is not None:
                if ev.kind == EventType.START:
                    rt.recording = True
                    rt.reconnecting = False
                    rt.record_started_at = ev.ts
                    rt.status = STATUS_RECORDING
                    rt.error = ""
                    self._queue.pop(item_id, None)
                    self._manual_queue.discard(item_id)
                elif ev.kind == EventType.RECONNECT:
                    rt.reconnecting = True
                    rt.status = STATUS_RECONNECTING
                    # 只统计真正断流/卡死事件；"第 N 次重连" 只是重试开始标记，不重复计数
                    if _looks_like_disconnect(ev.message):
                        degrade_action = self._maybe_note_reconnect_locked(item_id, rt)
                elif ev.kind == EventType.PROGRESS:
                    rt.duration_sec = ev.duration
                    rt.file_size = ev.size
                elif ev.kind == EventType.SEGMENT:
                    rt.file_size = max(rt.file_size, ev.size)
                    rt.segment_count += 1
                elif ev.kind == EventType.ERROR:
                    rt.error = ev.message
                    rt.status = STATUS_ERROR
                elif ev.kind == EventType.STOPPED:
                    rt.recording = False
                    rt.reconnecting = False
                    rt.duration_sec = ev.duration
                    rt.file_size = ev.size
                    if ev.path:
                        rt.output_path = ev.path
                    rt.stop_reason = ev.message
                    rt.status = STATUS_LIVE if rt.is_live else STATUS_OFFLINE
        if degrade_action is not None:
            self._perform_degrade(*degrade_action)
        self._forward(ev)

    def _emit(self, kind: str, message: str = "", item: Optional[WatchItem] = None,
              **kwargs: Any) -> None:
        ev = RecordEvent(
            kind=kind,
            room_id=(item.room_key if item is not None else ""),
            name=(item.display_name if item is not None else ""),
            message=message,
            **kwargs,
        )
        self._forward(ev)

    def _forward(self, ev: RecordEvent) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(ev)
        except Exception:  # pragma: no cover
            self.log.exception("事件回调异常")

    def _cleanup_disk(self) -> None:
        keep_days = self.cfg.get_int("keep_days")
        keep_max = self.cfg.get_int("keep_max_files")
        if keep_days <= 0 and keep_max <= 0:
            return
        with self._lock:
            protected: List[Path] = []
            for rec in self._recorders.values():
                for p in rec.status()["files"]:
                    protected.append(Path(p))
        try:
            deleted, freed = diskmod.cleanup_policy(
                self.cfg.output_root, keep_days, keep_max, protect=protected,
            )
        except Exception as exc:  # pragma: no cover
            self.log.warning("清理旧录像异常: %s", exc)
            return
        if deleted:
            self._emit(
                EventType.INFO,
                "清理旧录像 %d 个，释放 %s" % (deleted, diskmod.human_size(freed)),
            )

    # ==================================================================
    # 手动操作（GUI 按钮）
    # ==================================================================
    def force_check(self) -> None:
        """让下一轮调度立即查询所有房间（唤醒调度线程）。"""
        now = time.monotonic()
        with self._lock:
            for item_id in list(self._scheduled.keys()):
                self._schedule_locked(item_id, now)
            for item in self.watchlist.enabled():
                if item.id not in self._scheduled:
                    self._schedule_locked(item.id, now)
            for rt in self._states.values():
                rt.next_check_at = 0.0
        self._wake_event.set()

    def manual_record(self, item_id: str) -> Tuple[bool, str]:
        """手动开始录制某个房间（即使未开启自动录制；仍受并发闸门约束）。"""
        item = self.watchlist.get(item_id)
        if item is None:
            return False, "主播不存在"
        with self._lock:
            if item_id in self._recorders:
                return False, "该主播正在录制"
        if not self._ffmpeg:
            ok, ffmpeg, info = ffmpeg_util.check_ffmpeg(self.cfg.get_str("ffmpeg_path"))
            if not ok:
                return False, info
            self._ffmpeg = ffmpeg
        st = self.checker.check(item)
        self._apply_check(item, st)
        if st.error:
            return False, "查询失败：" + st.error
        if not st.is_live:
            return False, "主播当前未开播"
        with self._lock:
            self._last_status[item.id] = st
            self._manual_queue.add(item.id)
        estimated = estimate_mbps(item.quality or self.cfg.get_str("quality"))
        with self._lock:
            active = len(self._recorders)
            used_mbps = sum(self._recorder_mbps.values())
        reason = self._gate_block_reason(active, used_mbps, estimated)
        if reason:
            self._set_queued(item, reason, estimated)
            self._emit(EventType.INFO,
                       "手动录制已加入排队（%s）：%s" % (item.display_name, reason),
                       item=item)
            return True, "已加入排队：" + reason
        if self._start_recording(item, st):
            return True, "已开始录制"
        return False, "启动录制失败，详见日志"

    def manual_stop(self, item_id: str) -> Tuple[bool, str]:
        """手动停止某个房间的录制（或取消排队）。"""
        with self._lock:
            recording = item_id in self._recorders
            queued = item_id in self._queue
            rt = self._states.get(item_id)
        if recording:
            self._stop_recording(item_id, "手动停止")
            return True, "已请求停止录制"
        if queued:
            self._clear_queue(item_id)
            if rt is not None and not rt.error:
                rt.status = STATUS_LIVE if rt.is_live else STATUS_OFFLINE
            return True, "已取消排队"
        return False, "该主播当前没有在录制"
