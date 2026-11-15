# -*- coding: utf-8 -*-
"""浸泡测试入口：mock 1000+ 主播 / 真实 1360 主播。

mock 模式（默认）：
    python tests/soak/run_soak.py --mode mock --anchors 1000 --live-ratio 0.1 \
        --hours 0.5 --concurrent 10 --interval 60

    - 本机启动假抖音接口（tests/soak/fake_douyin.py，真实响应体/gzip/延迟/错误注入）；
    - 通过 checker 注入 FakeDouyinApi（不改 src/dy_api.py、不改 manager）；
    - 1000 个假主播共用本机 HTTP-FLV 测试流（src/mock_api.py 提供的流）；
    - 每场录制：ts -> remux mp4 -> ffmpeg 校验 Duration>0+有视频 -> 校验通过立即删除
      mp4 与源文件（校验失败保留到 out_keep 便于排查）；
    - 每 10s 采样 RSS/CPU/句柄/线程/ffmpeg 子进程/请求数/成功率/在录数/磁盘增量写 CSV。

real 模式：
    python tests/soak/run_soak.py --mode real --sample 200 --rate 2 --concurrency 4 \
        --record-n 3 --record-seconds 20

    - 读取 analysis/lead/watchlist_test_1360.json（1360 个主播主页链接）；
    - 限速轮询（--rate 每秒请求数 + --concurrency 并发），统计解析成功率/在播数/失败原因；
    - 对前 N 个在播房间录 20s -> 校验 -> 删除。
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SOAK_DIR = Path(__file__).resolve().parent
ROOT = SOAK_DIR.parent.parent
for p in (str(SOAK_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fake_douyin import FakeDouyin  # noqa: E402
from metrics import MetricsRecorder, dir_stats  # noqa: E402
from product_policy import ProductPolicy  # noqa: E402
from stream_server import StreamServer  # noqa: E402

from src import ffmpeg_util  # noqa: E402
from src.config import Config  # noqa: E402
from src.model import EventType, WatchItem  # noqa: E402

try:
    from src.model import WatchItem as _WatchItem
except ImportError:  # pragma: no cover
    _WatchItem = WatchItem
from src.storage import Watchlist  # noqa: E402

LOG = logging.getLogger("soak")


# ---------------------------------------------------------------------------
# 假协议层（对 checker 的契约实现，HTTP 走 fake_douyin 服务）
# ---------------------------------------------------------------------------
@dataclass
class FakeStatus:
    is_live: bool = False
    room_id: str = ""
    title: str = ""
    anchor_name: str = ""
    cover_url: str = ""
    started_at: int = 0
    streams: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    error: str = ""


def extract_rid(text: str) -> str:
    """从 target 中取第一段 5 位以上连续数字作为 web_rid。"""
    digits = ""
    for ch in str(text or ""):
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return digits if len(digits) >= 5 else ""


class FakeDouyinApi:
    """按 checker 契约实现，通过 HTTP 请求 fake_douyin 服务（模拟真实网络开销）。"""

    def __init__(self, base_url: str, stream_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.stream_url = stream_url
        self.timeout = timeout

    def _http_get(self, path: str, timeout: float) -> Tuple[int, bytes]:
        url = self.base_url + path
        req = urllib.request.Request(
            url,
            headers={"Accept-Encoding": "gzip", "User-Agent": "SoakHarness/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            return int(getattr(resp, "status", 200)), raw

    def get_live_status(self, target, cookie=None, timeout: float = 15) -> FakeStatus:
        rid = extract_rid(target)
        if not rid:
            return FakeStatus(is_live=False, error="fake_no_rid")
        try:
            status, raw = self._http_get(
                "/webcast/room/web/enter/?web_rid=" + rid,
                timeout=min(self.timeout, timeout or self.timeout),
            )
        except Exception as exc:
            return FakeStatus(is_live=False, room_id=rid,
                              error="fake_net:%s" % type(exc).__name__)
        if status != 200:
            return FakeStatus(is_live=False, room_id=rid, error="fake_http_%d" % status)
        if not raw:
            return FakeStatus(is_live=False, room_id=rid, error="fake_empty")
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return FakeStatus(is_live=False, room_id=rid, error="fake_bad_json")
        if isinstance(obj, dict) and obj.get("status_code") == 4001038:
            return FakeStatus(is_live=False, room_id=rid, error="fake_risk_4001038")
        rooms = ((obj.get("data") or {}).get("data") or []) if isinstance(obj, dict) else []
        if not rooms:
            return FakeStatus(is_live=False, room_id=rid, error="fake_no_room")
        room = rooms[0] or {}
        live = room.get("status") == 2
        owner = room.get("owner") or {}
        streams: Dict[str, list] = {}
        if live:
            stream_url = room.get("stream_url") or {}
            flv = list((stream_url.get("flv_pull_url") or {}).values())
            hls = list((stream_url.get("hls_pull_url_map") or {}).values())
            streams = {"flv": [self.stream_url] if self.stream_url else flv, "hls": hls}
        return FakeStatus(
            is_live=live,
            room_id=rid,
            title=str(room.get("title") or ""),
            anchor_name=str(owner.get("nickname") or ""),
            streams=streams,
        )

    def resolve_target(self, text: str) -> dict:
        rid = extract_rid(text)
        return {"web_rid": rid or None, "sec_uid": None, "url": ""}

    def choose_stream(self, status, prefer: str = "flv", quality: str = "origin") -> Optional[str]:
        if not getattr(status, "is_live", False):
            return None
        return self.stream_url or None

    def get_cookie(self) -> dict:
        return {"ttwid": "soak-fake-ttwid"}


# ---------------------------------------------------------------------------
# 事件统计 + 产物处理调度
# ---------------------------------------------------------------------------
class SoakEvents:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counts: Dict[str, int] = {}
        self.record_queue: queue.Queue = queue.Queue()
        self.errors: List[str] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.policy: Optional[ProductPolicy] = None

    def on_event(self, ev) -> None:
        with self.lock:
            self.counts[ev.kind] = self.counts.get(ev.kind, 0) + 1
            if ev.kind == EventType.ERROR and len(self.errors) < 200:
                self.errors.append(ev.message[:200])
        if ev.kind == EventType.STOPPED and ev.path:
            self.record_queue.put(ev.path)

    def start(self, policy: ProductPolicy) -> None:
        self.policy = policy
        self._thread = threading.Thread(target=self._dispatch, name="soak-dispatch", daemon=True)
        self._thread.start()

    def _dispatch(self) -> None:
        while not self._stop.is_set() or not self.record_queue.empty():
            try:
                path = self.record_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if self.policy is not None:
                    self.policy.submit(path)
            finally:
                self.record_queue.task_done()

    def stop(self, drain_timeout: float = 60.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(drain_timeout)

    def queued(self) -> int:
        return self.record_queue.qsize()

    def snapshot(self) -> Dict[str, int]:
        with self.lock:
            return dict(self.counts)


# ---------------------------------------------------------------------------
# 参数 / 配置
# ---------------------------------------------------------------------------
def parse_value(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抖音录制工具浸泡测试")
    parser.add_argument("--mode", choices=("mock", "real"), default="mock")
    parser.add_argument("--anchors", type=int, default=1000, help="mock 假主播数量")
    parser.add_argument("--live-ratio", type=float, default=0.1, help="稳态在播比例")
    parser.add_argument("--hours", type=float, default=0.5, help="运行时长（小时，可为小数）")
    parser.add_argument("--concurrency", "--concurrent", dest="concurrency", type=int,
                        default=10, help="mock: 同时录制上限；real: 并发查询数")
    parser.add_argument("--interval", type=int, default=60, help="状态轮询间隔（秒）")
    parser.add_argument("--error-rate", type=float, default=0.03, help="接口错误注入概率")
    parser.add_argument("--latency-min", type=float, default=0.15)
    parser.add_argument("--latency-max", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--stream-seconds", type=int, default=120, help="共享测试流时长")
    parser.add_argument("--record-timeout-min", type=float, default=1.0,
                        help="单场录制时长上限（分钟），用于制造录制循环")
    parser.add_argument("--tag", default="", help="输出文件后缀标记")
    parser.add_argument("--metrics-interval", type=float, default=10.0)
    parser.add_argument("--summary-every", type=float, default=600.0)
    parser.add_argument("--sweep-interval", type=float, default=120.0)
    parser.add_argument("--sweep-older", type=float, default=300.0)
    parser.add_argument("--drain-timeout", type=float, default=300.0)
    parser.add_argument("--config-set", action="append", default=[],
                        help="覆盖任意 config 字段，如 --config-set poll_workers=16")
    # real 模式参数
    parser.add_argument("--sample", type=int, default=200, help="real 模式抽样数量，0=全部 1360")
    parser.add_argument("--rate", type=float, default=2.0, help="real 模式每秒提交请求数上限")
    parser.add_argument("--rounds", type=int, default=1, help="real 模式轮询轮数")
    parser.add_argument("--round-interval", type=float, default=120.0, help="real 模式轮次间隔（秒）")
    parser.add_argument("--record-n", type=int, default=3, help="real 模式录制在播房间数")
    parser.add_argument("--record-seconds", type=float, default=20.0, help="real 模式单场录制秒数")
    parser.add_argument("--watchlist", default=str(ROOT / "analysis" / "lead" / "watchlist_test_1360.json"))
    parser.add_argument("--out-root", default=str(SOAK_DIR / "out"))
    parser.add_argument("--keep-dir", default=str(SOAK_DIR / "out_keep"))
    parser.add_argument("--log-dir", default=str(SOAK_DIR / "logs"))
    parser.add_argument("--tmp-dir", default=str(SOAK_DIR / "tmp"))
    return parser


def setup_logging(args) -> None:
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_dir / "soak.log"), encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def build_config(args, tag: str) -> Config:
    tmp = Path(args.tmp_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = Config.load(tmp / ("config_soak_%s.json" % tag))
    overrides = {
        "check_interval": args.interval,
        "max_concurrent": args.concurrency,
        "output_dir": str(Path(args.out_root)),
        "output_template": "{anchor}/{date}",
        "filename_template": "{anchor}_{date}_{time}",
        "container": "ts",
        "remux_mp4": False,
        "keep_source": False,
        "segment_duration": 0,
        "segment_size_mb": 0,
        "retry_max": 2,
        "retry_interval": 5,
        "retry_interval_max": 60,
        "stall_check_seconds": 30,
        "stall_kill_after": 2,
        "min_free_gb": 1.0,
        # 默认 30s 以下的录制会被 recorder 自动删除（防止空文件），
        # 浸泡测试里设为 5s，保证 real 模式 20s 的样本能进入 remux/校验/删除流程
        "min_keep_seconds": 5,
        "record_timeout_min": int(max(1, round(args.record_timeout_min))),
        "graceful_stop_timeout": 20,
        "use_mock_api": False,
        "log_dir": str(Path(args.log_dir)),
        "log_level": "WARNING",
        "ui_refresh_ms": 1000,
        "notify": {"enabled": False, "webhooks": [], "sound": False, "events": []},
    }
    cfg.update(overrides)
    for raw in args.config_set:
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        cfg[key.strip()] = parse_value(value)
    cfg._normalize()
    cfg.save()
    cfg.ensure_dirs()
    return cfg


def build_mock_watchlist(args) -> Watchlist:
    wl_path = Path(args.tmp_dir) / ("watchlist_mock_%d.json" % args.anchors)
    wl = Watchlist(wl_path)
    items = []
    for i in range(args.anchors):
        rid = str(10000000000 + i)
        items.append(WatchItem(
            name="主播%04d" % i,
            target=rid,
            web_rid=rid,
            url="http://127.0.0.1/fake/" + rid,
            auto_record=True,
            quality="origin",
            enabled=True,
        ))
    wl._items = items
    wl.save()
    return wl


# ---------------------------------------------------------------------------
# mock 模式
# ---------------------------------------------------------------------------
def run_mock(args) -> Dict:
    start_wall = time.time()
    tag = args.tag or ("mock_%d_%s" % (args.anchors, time.strftime("%Y%m%d_%H%M%S")))
    out_root = Path(args.out_root)
    keep_dir = Path(args.keep_dir)
    log_dir = Path(args.log_dir)
    tmp = Path(args.tmp_dir)
    for p in (out_root, keep_dir, log_dir, tmp):
        p.mkdir(parents=True, exist_ok=True)

    ffmpeg, ffmpeg_err = ffmpeg_util.resolve_ffmpeg("")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg: " + ffmpeg_err)
    LOG.info("ffmpeg: %s", ffmpeg)

    cfg = build_config(args, tag)

    # 1) 假抖音服务 + 共享测试流
    fake = FakeDouyin(
        anchors=args.anchors, live_ratio=args.live_ratio,
        latency_min=args.latency_min, latency_max=args.latency_max,
        error_rate=args.error_rate, seed=args.seed,
    )
    port = fake.start()
    LOG.info("fake douyin: http://127.0.0.1:%d anchors=%d live_ratio=%.2f err_rate=%.2f",
             port, args.anchors, args.live_ratio, args.error_rate)

    stream = StreamServer(ffmpeg, tmp / "stream", seconds=args.stream_seconds)
    stream_url = stream.start()
    LOG.info("共享测试流: %s", stream_url)

    api = FakeDouyinApi("http://127.0.0.1:%d" % port, stream_url, timeout=10.0)
    import src.checker as checker_mod
    checker_mod.load_api = lambda use_mock=False: (api, "fake_douyin", "")

    # 2) watchlist + manager
    wl = build_mock_watchlist(args)
    events = SoakEvents()
    from src.manager import MonitorManager
    manager = MonitorManager(cfg, wl, on_event=events.on_event)
    # 兜底：如果 manager 自行创建了别的 checker，强制替换（适配 perf-dev 重构）
    try:
        manager.checker.api = api
        manager.checker.api_name = "fake_douyin"
        manager.checker.api_error = ""
    except Exception:
        pass
    LOG.info("协议层: %s", manager.checker.describe())

    policy = ProductPolicy(
        ffmpeg, keep_dir,
        log_dir / ("product_%s.jsonl" % tag),
        workers=2, logger=logging.getLogger("soak.product"),
    )
    events.start(policy)

    metrics = MetricsRecorder(
        log_dir / ("metrics_%s.csv" % tag),
        interval=args.metrics_interval,
        summary_every=args.summary_every,
    )

    def provider() -> Dict[str, float]:
        try:
            fstats = fake.stats_snapshot()
        except Exception:
            fstats = {}
        try:
            rows = manager.snapshot()
        except Exception:
            rows = []
        try:
            mstats = manager.stats() if hasattr(manager, "stats") else {}
        except Exception:
            mstats = {}
        recording = sum(1 for r in rows if r.get("recording"))
        live = sum(1 for r in rows if r.get("is_live"))
        pstats = policy.stats_snapshot()
        ds = dir_stats(out_root)
        ev = events.snapshot()
        return {
            "req_total": fstats.get("requests", 0),
            "req_ok": fstats.get("ok", 0),
            "req_err": fstats.get("errors", 0),
            "live_rooms": live,
            "recordings": recording,
            "checks_done": fstats.get("ok", 0),
            "policy_queue": pstats.get("queue", 0),
            "disk_mb": ds["disk_mb"],
            "disk_files": ds["disk_files"],
            "remux_ok": pstats.get("remux_ok", 0),
            "remux_fail": pstats.get("remux_fail", 0),
            "events_started": ev.get(EventType.START, 0),
            "events_stopped": ev.get(EventType.STOPPED, 0),
            "queued_count": mstats.get("queued_count", 0),
            "risk_count": mstats.get("risk_count", 0),
            "avg_latency_ms": mstats.get("avg_latency_ms", 0.0),
            "in_flight": mstats.get("in_flight", 0),
            "breaker_paused": 1 if mstats.get("breaker_state") == "open" else 0,
        }

    metrics.provider = provider
    duration = max(10.0, args.hours * 3600.0)
    print("=" * 78, flush=True)
    print("浸泡测试开始 mode=mock anchors=%d live_ratio=%.2f concurrent=%d interval=%ds "
          "duration=%.1fmin" % (args.anchors, args.live_ratio, args.concurrency,
                                args.interval, duration / 60.0), flush=True)
    print("输出: %s  CSV: %s" % (out_root, metrics.csv_path), flush=True)
    print("=" * 78, flush=True)

    started = False
    interrupted = False
    try:
        ok, msg = manager.start()
        if not ok:
            raise RuntimeError("manager 启动失败: " + msg)
        started = True
        metrics.start()
        t0 = time.time()
        next_sweep = t0 + args.sweep_interval
        while time.time() - t0 < duration:
            time.sleep(5.0)
            if time.time() >= next_sweep:
                next_sweep = time.time() + args.sweep_interval
                try:
                    count = policy.sweep(out_root, older_than=args.sweep_older)
                    if count:
                        LOG.warning("兜底清扫提交 %d 个残留文件", count)
                except Exception as exc:
                    LOG.warning("清扫异常: %s", exc)
    except KeyboardInterrupt:
        interrupted = True
        LOG.warning("收到 Ctrl+C，提前结束")
    finally:
        if started:
            LOG.info("停止监控（等待 ffmpeg 退出）...")
            manager.stop(wait=True, timeout=180)
        events.stop(drain_timeout=60)
        # 收尾清扫：把停止时仍在队列/目录里的产物也走 remux+校验+删除
        try:
            rest = policy.sweep(out_root, older_than=0)
            if rest:
                LOG.info("收尾清扫提交 %d 个残留文件", rest)
        except Exception as exc:
            LOG.warning("收尾清扫异常: %s", exc)
        policy.stop(drain_timeout=args.drain_timeout)
        metrics.stop()
        stream.stop()
        fake.stop()
        elapsed = time.time() - start_wall
        msummary = metrics.summary()
        pstats = policy.stats_snapshot()
        ev = events.snapshot()
        fstats = fake.stats_snapshot() if not fake._server is None else {}
        summary = {
            "mode": "mock",
            "tag": tag,
            "args": vars(args),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_wall)),
            "wall_seconds": round(elapsed, 1),
            "interrupted": interrupted,
            "config": {k: cfg.get(k) for k in (
                "check_interval", "max_concurrent", "container", "remux_mp4",
                "record_timeout_min", "retry_max", "stall_check_seconds",
                "segment_duration", "keep_days", "min_free_gb")},
            "metrics": msummary,
            "event_counts": ev,
            "policy": pstats,
            "fake_stats": fstats,
            "errors_sample": events.errors[:50],
        }
        summary_path = log_dir / ("summary_%s.json" % tag)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        _print_summary(summary)
        print("摘要 JSON: %s" % summary_path, flush=True)
        return summary


def _print_summary(summary: Dict) -> None:
    m = summary.get("metrics") or {}
    p = summary.get("policy") or {}
    ev = summary.get("event_counts") or {}
    print()
    print("=" * 78)
    print("浸泡测试汇总 (%s, 运行 %.1f 分钟)" % (
        summary.get("tag"), (summary.get("wall_seconds") or 0) / 60.0))
    print("-" * 78)
    if m:
        print("RSS       : 均值 %.1f MB / 峰值 %.1f MB（首 %.1f → 末 %.1f，漂移 %+.1f MB/h）" % (
            m["rss_mb"]["mean"], m["rss_mb"]["max"], m["rss_first_mb"],
            m["rss_last_mb"], m["rss_drift_mb_per_h"]))
        print("CPU       : 均值 %.1f%% / 峰值 %.1f%%" % (
            m["cpu_pct"]["mean"], m["cpu_pct"]["max"]))
        print("句柄      : 均值 %.0f / 峰值 %.0f" % (
            m["handles"]["mean"], m["handles"]["max"]))
        print("线程      : 均值 %.1f / 峰值 %.0f" % (
            m["py_threads"]["mean"], m["py_threads"]["max"]))
        print("ffmpeg 子进程: 均值 %.1f / 峰值 %.0f" % (
            m["ffmpeg_procs"]["mean"], m["ffmpeg_procs"]["max"]))
        print("在录房间  : 均值 %.1f / 峰值 %.0f" % (
            m["recordings"]["mean"], m["recordings"]["max"]))
        print("直播房间  : 均值 %.1f" % m["live_rooms"]["mean"])
        print("请求      : 总计 %s，成功率 %s%%" % (
            m.get("req_total"), m.get("req_success_rate")))
        print("磁盘占用  : 峰值 %.1f MB" % m["disk_mb"]["max"])
    if m:
        print("排队/风控 : 排队峰值 %s，风控计数峰值 %s，平均延迟峰值 %.1f ms，熔断暂停样本 %s" % (
            m.get("queued_count", {}).get("max", 0),
            m.get("risk_count", {}).get("max", 0),
            m.get("avg_latency_ms", {}).get("max", 0),
            m.get("breaker_paused", {}).get("sum", "?")))
    print("事件      : start=%s stopped=%s error=%s reconnect=%s" % (
        ev.get("start", 0), ev.get("stopped", 0), ev.get("error", 0), ev.get("reconnect", 0)))
    print("产物策略  : 处理 %s（成功删除 %s / 失败保留 %s / 跳过 %s），删除文件 %s 个 %.1f MB，remux ok=%s fail=%s" % (
        p.get("processed"), p.get("ok"), p.get("fail"), p.get("skipped"),
        p.get("deleted_files"), (p.get("deleted_bytes") or 0) / 1048576.0,
        p.get("remux_ok"), p.get("remux_fail")))
    fake = summary.get("fake_stats") or {}
    if fake:
        print("假接口    : 请求 %s，ok=%s err=%s，注入错误分布 %s" % (
            fake.get("requests"), fake.get("ok"), fake.get("errors"),
            fake.get("error_kinds")))
    print("=" * 78)


# ---------------------------------------------------------------------------
# real 模式
# ---------------------------------------------------------------------------
def _normalize_error(text: str) -> str:
    """把 dy_api 的失败原因归类，便于统计分布。"""
    if not text:
        return "unknown"
    lowered = text.lower()
    if "4001038" in lowered:
        return "risk_4001038"
    if "无房间数据" in text:
        return "enter_无房间数据"
    if "没能解析出直播间" in text or "无法解析" in text or "解析失败" in text:
        return "resolve_failed"
    if "超时" in text or "timeout" in lowered:
        return "timeout"
    if "403" in text:
        return "http_403"
    if "404" in text:
        return "http_404"
    if "500" in text or "502" in text or "503" in text:
        return "http_5xx"
    if "风控" in text or "验证" in text or "captcha" in lowered:
        return "risk_other"
    if "empty" in lowered or "空" in text:
        return "empty_response"
    if "http" in lowered or "网络" in text or "connection" in lowered:
        return "network"
    return "other"


def load_real_items(path: Path) -> List[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("items") or [])
    return list(data or [])


def run_real(args) -> Dict:
    from src.checker import LiveChecker
    from src.recorder import RoomRecorder

    start_wall = time.time()
    tag = args.tag or ("real_%s" % time.strftime("%Y%m%d_%H%M%S"))
    out_root = Path(args.out_root) / "real"
    keep_dir = Path(args.keep_dir) / "real"
    log_dir = Path(args.log_dir)
    tmp = Path(args.tmp_dir)
    for p in (out_root, keep_dir, log_dir, tmp):
        p.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffmpeg_err = ffmpeg_util.resolve_ffmpeg("")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg: " + ffmpeg_err)

    cfg = build_config(args, "real_" + tag)
    cfg["output_dir"] = str(out_root)
    cfg["record_timeout_min"] = 1
    cfg.save()
    checker = LiveChecker(cfg)
    LOG.info("协议层: %s", checker.describe())

    items = load_real_items(Path(args.watchlist))
    rng = random.Random(args.seed)
    if args.sample and args.sample < len(items):
        sample = rng.sample(items, args.sample)
    else:
        sample = list(items)
    LOG.info("真实主播目录: %d 条，抽样 %d 条，rate=%.1f/s concurrency=%d rounds=%d",
             len(items), len(sample), args.rate, args.concurrency, args.rounds)

    all_results: List[Dict] = []
    for round_idx in range(max(1, args.rounds)):
        if round_idx:
            LOG.info("等待 %d 秒进入下一轮...", int(args.round_interval))
            time.sleep(args.round_interval)
        LOG.info("===== 第 %d/%d 轮开始 =====", round_idx + 1, args.rounds)
        results: List[Tuple[dict, Any]] = []
        lock = threading.Lock()

        def work(item: dict) -> None:
            # checker 需要 WatchItem 或字符串，不能直接吃 dict
            try:
                watch_item = WatchItem.from_dict(item)
            except Exception:
                watch_item = item
            st = checker.check(watch_item)
            with lock:
                results.append((item, st))

        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
            futures = []
            for item in sample:
                futures.append(executor.submit(work, item))
                time.sleep(1.0 / max(0.1, args.rate))
            for fut in futures:
                try:
                    fut.result(timeout=120)
                except Exception as exc:
                    LOG.warning("单条查询异常: %s", exc)
        all_results = [{"name": item.get("name", ""), "target": item.get("target", ""),
                        "is_live": bool(st.is_live), "error": st.error,
                        "room_id": st.room_id, "title": st.title}
                       for item, st in results]
        live = sum(1 for r in all_results if r["is_live"])
        errors = [r for r in all_results if r["error"]]
        LOG.info("轮次 %d: 查询 %d，直播中 %d（%.1f%%），失败 %d（%.1f%%）",
                 round_idx + 1, len(all_results), live,
                 live / max(1, len(all_results)) * 100.0, len(errors),
                 len(errors) / max(1, len(all_results)) * 100.0)
        if round_idx < args.rounds - 1:
            time.sleep(max(0.0, args.round_interval))

    live_count = sum(1 for r in all_results if r["is_live"])
    error_count = sum(1 for r in all_results if r["error"])
    error_dist: Dict[str, int] = {}
    for r in all_results:
        if r["error"]:
            key = _normalize_error(r["error"])
            error_dist[key] = error_dist.get(key, 0) + 1

    # 对在播房间录制 20s -> 校验 -> 删除
    policy = ProductPolicy(ffmpeg, keep_dir, log_dir / ("product_%s.jsonl" % tag),
                           workers=1, logger=logging.getLogger("soak.product"))
    record_results: List[Dict] = []
    live_rows = [r for r in all_results if r["is_live"]]
    rng.shuffle(live_rows)
    for row in live_rows[: max(0, args.record_n)]:
        try:
            st = checker.check(row["target"])
            url = checker.choose_url(st, "origin")
            if not url:
                record_results.append({"target": row["target"], "ok": False,
                                       "reason": "无流地址"})
                continue
            room_dir = out_root / ("real_" + (st.room_id or extract_rid(row["target"]) or "room"))
            room_dir.mkdir(parents=True, exist_ok=True)
            base = "real_%s_%s" % (st.room_id or "room", time.strftime("%H%M%S"))
            rec = RoomRecorder(
                ffmpeg, url, room_dir, base,
                name=row.get("name") or st.anchor_name or "real",
                room_id=st.room_id or "",
                container="ts", remux_mp4=False,
                retry_max=1, retry_interval=3,
                record_timeout_min=1,
                stall_check_seconds=20, stall_kill_after=2,
            )
            rec.start()
            time.sleep(args.record_seconds)
            rec.stop("real 浸泡测试")
            rec.join(90)
            status = rec.status()
            processed = []
            for f in status.get("files") or []:
                processed.append(policy.process_sync(f))
            ok = all(p.get("ok") for p in processed) if processed else False
            record_results.append({
                "target": row["target"],
                "room_id": st.room_id,
                "duration": status.get("duration"),
                "size_mb": round((status.get("file_size") or 0) / 1048576.0, 2),
                "ok": ok,
                "processed": processed,
            })
            LOG.info("真实录制 %s: 时长 %ss 大小 %.1fMB 校验删除=%s",
                     row["target"][:60], status.get("duration"),
                     (status.get("file_size") or 0) / 1048576.0, ok)
        except Exception as exc:
            LOG.exception("真实录制异常: %s", exc)
            record_results.append({"target": row.get("target", ""), "ok": False,
                                   "reason": str(exc)[:200]})
    policy.stop(drain_timeout=120)

    summary = {
        "mode": "real",
        "tag": tag,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_wall)),
        "wall_seconds": round(time.time() - start_wall, 1),
        "watchlist_total": len(items),
        "sampled": len(all_results),
        "rounds": args.rounds,
        "rate": args.rate,
        "concurrency": args.concurrency,
        "live_count": live_count,
        "live_ratio": round(live_count / max(1, len(all_results)), 4),
        "error_count": error_count,
        "error_dist": error_dist,
        "policy": policy.stats_snapshot(),
        "record_results": record_results,
        "results_sample": all_results[:200],
    }
    summary_path = log_dir / ("real_summary_%s.json" % tag)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("=" * 78)
    print("真实模式汇总: 抽样 %d，直播中 %d（%.1f%%），失败 %d（%.1f%%）" % (
        len(all_results), live_count, live_count / max(1, len(all_results)) * 100.0,
        error_count, error_count / max(1, len(all_results)) * 100.0))
    print("失败原因分布: %s" % error_dist)
    print("录制校验删除: %s" % [(r.get("room_id"), r.get("ok")) for r in record_results])
    print("产物策略: %s" % summary["policy"])
    print("摘要 JSON: %s" % summary_path)
    print("=" * 78)
    return summary


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args)
    try:
        if args.mode == "real":
            run_real(args)
        else:
            run_mock(args)
    except KeyboardInterrupt:
        LOG.warning("中断")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
