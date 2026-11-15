# -*- coding: utf-8 -*-
"""P1 调度器测试：令牌桶 / 自适应档位 / 熔断 / 批量导入线程 / 并发闸门。

不依赖 GUI，不访问真实抖音（用 mock 直播源或假 checker）。

运行：python tests/test_scheduler.py
"""
import json
import logging
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src import manager as manager_mod
from src.checker import StatusView
from src.config import Config, DEFAULTS
from src.manager import (
    CircuitBreaker,
    MonitorManager,
    STATUS_QUEUED,
    TokenBucket,
    estimate_mbps,
)
from src.mock_api import configure as mock_configure
from src.model import WatchItem
from src.storage import Watchlist

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
LOG = logging.getLogger("test.scheduler")

TMP = ROOT / "tests" / "tmp" / "scheduler"


def reset_dir(name: str = "") -> Path:
    path = TMP / name if name else TMP
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def wait_until(cond, timeout, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def make_manager(name: str, item_count: int = 3, **overrides):
    """构造一个使用 mock 协议层的 Manager（不启动）。"""
    work = reset_dir(name)
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    cfg = Config.load(work / "config.json")
    cfg["output_dir"] = str(out)
    cfg["use_mock_api"] = True
    cfg["remux_mp4"] = False
    cfg["min_keep_seconds"] = 0
    cfg["keep_days"] = 0
    cfg["min_free_gb"] = 0
    cfg["poll_interval_live"] = 1
    cfg["poll_interval_warm"] = 1
    cfg["poll_interval_cold"] = 1
    cfg["poll_rate_limit"] = 50
    cfg["max_poll_workers"] = 4
    for key, value in overrides.items():
        cfg[key] = value
    wl = Watchlist(work / "watchlist.json")
    wl.clear()
    for index in range(item_count):
        wl.add_from_text("1%011d" % (index + 1), name="主播%d" % (index + 1),
                         quality=cfg.get_str("quality"))
    mock_configure(ffmpeg_path=cfg.get_str("ffmpeg_path"), live_seconds=60,
                   offline_after=0, disconnect_after=0)
    mgr = MonitorManager(cfg, wl)
    return cfg, wl, mgr, out


# ---------------------------------------------------------------------------
# 配置迁移 / 校验
# ---------------------------------------------------------------------------
def test_config_migration():
    work = reset_dir("config")
    old = work / "old_config.json"
    old.write_text(json.dumps({"max_concurrent": 3, "check_interval": 60},
                              ensure_ascii=False), encoding="utf-8")
    cfg = Config.load(old)
    LOG.info("老配置迁移后 max_concurrent=%s poll_rate_limit=%s",
             cfg["max_concurrent"], cfg["poll_rate_limit"])
    assert cfg["max_concurrent"] == 0, "旧默认 3 应迁移为 0=不限"
    assert cfg["max_poll_workers"] == 12 and cfg["poll_rate_limit"] == 6
    saved = json.loads(old.read_text(encoding="utf-8"))
    for key in ("max_poll_workers", "poll_interval_live", "breaker_error_rate",
                "resolve_workers", "max_total_mbps", "max_ffmpeg_processes"):
        assert key in saved, "新配置项未自动写入 config.json: %s" % key

    explicit = work / "explicit.json"
    data = dict(DEFAULTS)
    data["max_poll_workers"] = 12
    data["max_concurrent"] = 3
    explicit.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert Config.load(explicit)["max_concurrent"] == 3, "用户显式设置的值不应被迁移覆盖"

    bad = work / "bad.json"
    data = dict(DEFAULTS)
    data["max_poll_workers"] = 12
    data.update({"poll_rate_limit": 0, "breaker_error_rate": 5.0,
                 "poll_backoff_start": 30, "poll_backoff_max": 5,
                 "max_concurrent": -1, "breaker_pause_start": 40,
                 "breaker_pause_max": 10})
    bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = Config.load(bad)
    assert cfg["poll_rate_limit"] == 1, cfg["poll_rate_limit"]
    assert cfg["breaker_error_rate"] == 1.0
    assert cfg["poll_backoff_max"] == 30
    assert cfg["breaker_pause_max"] == 40
    assert cfg["max_concurrent"] == 0
    return True


# ---------------------------------------------------------------------------
# 令牌桶
# ---------------------------------------------------------------------------
def test_token_bucket_rate():
    clock = [0.0]
    import random
    bucket = TokenBucket(10, burst=1, jitter=0.2, clock=lambda: clock[0],
                         rng=random.Random(7))
    now = 0.0
    count = 0
    while now < 10.0:
        now += 0.001
        clock[0] = now
        if bucket.try_acquire(now):
            count += 1
    LOG.info("10 秒内令牌数=%d（期望约 100，±20%%）", count)
    assert 75 <= count <= 125, count
    # 桶空时 wait_time 合理
    clock[0] = 100.0
    assert bucket.try_acquire(100.0) in (True, False)
    wait = bucket.wait_time(100.0)
    assert 0.0 <= wait <= 1.0, wait
    return True


# ---------------------------------------------------------------------------
# 自适应档位 / 失败退避
# ---------------------------------------------------------------------------
def test_adaptive_intervals():
    cfg, wl, mgr, _out = make_manager("intervals", item_count=0,
                                      poll_interval_live=30, poll_interval_warm=90,
                                      poll_interval_cold=300)
    try:
        now = time.time()
        assert mgr.poll_interval_for("a", True, now=now) == 30
        mgr._last_live_at["b"] = now - 3600
        assert mgr.poll_interval_for("b", False, now=now) == 90
        mgr._last_live_at["c"] = now - 3 * 86400
        assert mgr.poll_interval_for("c", False, now=now) == 300
        assert mgr.poll_interval_for("d", False, now=now) == 300
        backoff = [int(mgr.poll_interval_for("e", False, failures=n)) for n in (1, 2, 3, 4, 5)]
        LOG.info("失败退避序列: %s", backoff)
        assert backoff == [30, 60, 120, 240, 300], backoff
    finally:
        mgr._shutdown_workers()
    return True


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------
def test_circuit_breaker():
    clock = [0.0]
    breaker = CircuitBreaker(error_rate=0.3, window_seconds=10, min_requests=4,
                             pause_start=30, pause_max=120, halfopen_requests=3,
                             clock=lambda: clock[0])
    # 第 1 个窗口：4 错 + 1 成功 -> 错误率 80% -> 记 1 次
    clock[0] = 1.0
    for _ in range(4):
        breaker.record(False, 0.1, now=clock[0])
    clock[0] = 11.0
    breaker.record(True, 0.1, now=11.0)
    assert breaker.state == CircuitBreaker.CLOSED, breaker.state
    # 第 2 个窗口：再来 4 错 1 成功 -> 连续 2 窗口 >30% -> 熔断
    clock[0] = 12.0
    for _ in range(4):
        breaker.record(False, 0.1, now=12.0)
    clock[0] = 22.0
    breaker.record(True, 0.1, now=22.0)
    LOG.info("熔断状态=%s 暂停=%.0fs", breaker.state, breaker.pause_seconds)
    assert breaker.state == CircuitBreaker.OPEN
    assert 30 <= breaker.pause_seconds <= 120
    assert breaker.allow(23.0) is False
    assert breaker.allow(22.0 + 30.0) is True
    assert breaker.state == CircuitBreaker.HALF_OPEN
    clock[0] = 52.0
    for _ in range(3):
        breaker.record(True, 0.1, now=52.0)
    assert breaker.state == CircuitBreaker.CLOSED, breaker.state

    # 半开阶段又失败 -> 再次熔断且暂停翻倍
    clock[0] = 100.0
    for _ in range(4):
        breaker.record(False, 0.1, now=100.0)
    clock[0] = 110.0
    breaker.record(True, 0.1, now=110.0)
    clock[0] = 111.0
    for _ in range(4):
        breaker.record(False, 0.1, now=111.0)
    clock[0] = 121.0
    breaker.record(True, 0.1, now=121.0)
    assert breaker.state == CircuitBreaker.OPEN
    first_pause = breaker.pause_seconds
    assert breaker.allow(122.0) is False
    assert breaker.allow(121.0 + first_pause) is True   # 进入半开
    breaker.record(False, 0.1, now=121.0 + first_pause)  # 半开失败 -> 再熔断
    assert breaker.state == CircuitBreaker.OPEN
    LOG.info("二次熔断暂停=%.0fs（首次 %.0fs）", breaker.pause_seconds, first_pause)
    assert breaker.pause_seconds >= min(120.0, first_pause * 2 - 0.01)
    return True


def test_cookie_refresh_and_breaker_trigger():
    cfg, wl, mgr, _out = make_manager("breaker", item_count=0)
    try:
        class _FakeApi:
            def __init__(self):
                self.calls = []

            def get_cookie(self, force=False):
                self.calls.append(force)
                return {"ttwid": "fake"}

        fake = _FakeApi()
        mgr.checker.api = fake
        assert mgr._refresh_cookie() is True
        assert fake.calls == [True], fake.calls

        class _OldApi:
            def get_cookie(self):
                return {"ttwid": "old"}

        mgr.checker.api = _OldApi()
        assert mgr._refresh_cookie() is True

        # 用一个快窗口熔断器验证 _record_poll_result 会触发刷新
        fake2 = _FakeApi()
        mgr.checker.api = fake2
        mgr._breaker = CircuitBreaker(error_rate=0.05, window_seconds=1.0,
                                      min_requests=1, pause_start=30,
                                      pause_max=120, halfopen_requests=2)
        mgr._record_poll_result(StatusView(is_live=False, error="风控验证失败"), 0.01)
        time.sleep(1.05)
        mgr._record_poll_result(StatusView(is_live=False, error="风控验证失败"), 0.01)
        time.sleep(1.05)
        mgr._record_poll_result(StatusView(is_live=False, error="风控验证失败"), 0.01)
        stats = mgr.stats()
        LOG.info("熔断触发: state=%s requests=%s risk=%s", stats["breaker_state"],
                 stats["requests"], stats["risk_count"])
        assert mgr._breaker.state == CircuitBreaker.OPEN
        assert fake2.calls and fake2.calls[-1] is True
        assert stats["risk_count"] >= 3
        assert stats["success_rate"] == 0.0
    finally:
        mgr._shutdown_workers()
    return True


# ---------------------------------------------------------------------------
# 并发闸门（单元）
# ---------------------------------------------------------------------------
def test_gate_reasons_unit():
    cfg, wl, mgr, _out = make_manager("gates", item_count=0)
    original = manager_mod.diskmod.has_enough_space
    try:
        cfg["max_concurrent"] = 2
        cfg["max_ffmpeg_processes"] = 3
        cfg["max_total_mbps"] = 10
        cfg["min_free_gb"] = 0
        assert mgr._gate_block_reason(0, 0.0, 6.4) == ""
        assert "并发上限" in mgr._gate_block_reason(2, 0.0, 6.4)
        cfg["max_concurrent"] = 0
        assert "ffmpeg 进程上限" in mgr._gate_block_reason(3, 0.0, 6.4)
        cfg["max_ffmpeg_processes"] = 0
        assert "带宽预算" in mgr._gate_block_reason(1, 6.4, 6.4)
        assert mgr._gate_block_reason(1, 6.4, 2.0) == ""
        manager_mod.diskmod.has_enough_space = lambda *a, **k: False
        assert "磁盘" in mgr._gate_block_reason(0, 0.0, 6.4)
        LOG.info("闸门原因：并发/进程/带宽/磁盘 均正确")
    finally:
        manager_mod.diskmod.has_enough_space = original
        mgr._shutdown_workers()
    return True


# ---------------------------------------------------------------------------
# 批量导入：1360 条不炸线程
# ---------------------------------------------------------------------------
def test_batch_import_1360_threads():
    cfg, wl, mgr, _out = make_manager("import", item_count=0,
                                      poll_rate_limit=2000, resolve_workers=3,
                                      resolve_retries=0)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "calls": 0}

    def fake_resolve(text):
        with lock:
            state["active"] += 1
            state["calls"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.002)
        with lock:
            state["active"] -= 1
        return {"web_rid": "123456789", "name": "主播", "url": str(text), "error": ""}

    mgr.checker.resolve = fake_resolve
    targets = ["https://www.douyin.com/user/MS4wLjABAAAAtest%06d" % i
               for i in range(1360)]
    baseline = threading.active_count()
    t0 = time.time()
    items = mgr.add_items(targets, auto_record=False)
    assert len(items) == 1360
    threads_after = threading.active_count()
    finished = wait_until(lambda: mgr.resolve_pending_count() == 0, 60)
    elapsed = time.time() - t0
    LOG.info("1360 条导入：耗时 %.2fs，线程 %d -> %d，解析并发峰值 %d，calls=%d",
             elapsed, baseline, threads_after, state["max_active"], state["calls"])
    assert finished, "1360 条解析未在超时内完成"
    assert state["calls"] >= 1360 * 0.95
    assert state["max_active"] <= 3, "解析并发超过 resolve_workers: %s" % state["max_active"]
    assert threads_after - baseline <= 6, "导入 1360 条线程增长过多: %d" % (threads_after - baseline)
    assert len(wl) == 1360
    mgr.flush()
    saved = json.loads((mgr.watchlist.path).read_text(encoding="utf-8"))
    assert len(saved.get("items") or []) == 1360
    mgr.stop(wait=True, timeout=10)
    return True


# ---------------------------------------------------------------------------
# 固定工作池：查询并发不超过 max_poll_workers
# ---------------------------------------------------------------------------
def test_poll_worker_pool():
    cfg, wl, mgr, _out = make_manager("pollpool", item_count=30,
                                      poll_rate_limit=1000, max_poll_workers=3,
                                      poll_interval_live=1000,
                                      poll_interval_warm=1000,
                                      poll_interval_cold=1000)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "calls": 0}

    def fake_check(item):
        with lock:
            state["active"] += 1
            state["calls"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1
        return StatusView(is_live=False, title="假")

    mgr.checker.check = fake_check
    baseline = threading.active_count()
    ok, msg = mgr.start()
    assert ok, msg
    try:
        assert wait_until(lambda: state["calls"] >= 10, 15)
        LOG.info("工作线程池：calls=%d 并发峰值=%d 线程增量=%d",
                 state["calls"], state["max_active"], threading.active_count() - baseline)
        assert state["max_active"] <= 3, "工作池并发超过 max_poll_workers: %d" % state["max_active"]
        stats = mgr.stats()
        assert stats["max_poll_workers"] == 3
        # calls 在 fake 开始时计数，requests 在完成后计数，允许少量在途差异
        assert 0 < stats["requests"] <= state["calls"]
        assert stats["requests"] >= state["calls"] - 3
        assert threading.active_count() - baseline <= 10
    finally:
        mgr.stop(wait=True, timeout=20)
    return True


# ---------------------------------------------------------------------------
# 并发闸门 + 排队 + 闸门释放自动开录（mock 直播源）
# ---------------------------------------------------------------------------
def test_gate_queue_and_release():
    cfg, wl, mgr, _out = make_manager("gate_integration", item_count=5,
                                      max_concurrent=1, max_ffmpeg_processes=300,
                                      max_total_mbps=0)
    ok, msg = mgr.start()
    assert ok, msg
    try:
        started = wait_until(lambda: sum(1 for r in mgr.snapshot() if r["recording"]) == 1, 25)
        assert started, "应只有 1 路开录"
        assert wait_until(lambda: sum(1 for r in mgr.snapshot() if r["queued"]) == 4, 10)
        rows = mgr.snapshot()
        recording = [r for r in rows if r["recording"]]
        queued = [r for r in rows if r["queued"]]
        assert len(recording) == 1 and len(queued) == 4
        for row in queued:
            assert row["status"] == STATUS_QUEUED, row["status"]
            assert "并发上限" in row["queue_reason"], row["queue_reason"]
            assert row["estimated_mbps"] == 6.4, row["estimated_mbps"]
        sample = rows[0]
        for key in ("queued", "queue_reason", "rec_count", "estimated_mbps",
                    "requests", "success_rate", "avg_latency_ms", "risk_count"):
            assert key in sample, "snapshot 缺少字段: %s" % key
        stats = mgr.stats()
        assert stats["rec_count"] == 1 and stats["queued_count"] == 4
        assert stats["requests"] > 0 and 0.0 <= stats["success_rate"] <= 1.0
        LOG.info("闸门生效：在录=1 排队=4 原因=%s", queued[0]["queue_reason"])

        # 放开并发上限 -> 自动开录
        cfg["max_concurrent"] = 5
        released = wait_until(lambda: sum(1 for r in mgr.snapshot() if r["recording"]) == 5, 25)
        assert released, "闸门释放后未自动开录: %s" % [(r["name"], r["status"])
                                                       for r in mgr.snapshot()]
        assert sum(1 for r in mgr.snapshot() if r["queued"]) == 0
        LOG.info("闸门释放后 5 路全部开录")
    finally:
        mgr.stop(wait=True, timeout=40)
    return True


def test_mbps_gate_integration():
    cfg, wl, mgr, _out = make_manager("mbps_gate", item_count=3,
                                      max_concurrent=0, max_ffmpeg_processes=300,
                                      max_total_mbps=8)
    ok, msg = mgr.start()
    assert ok, msg
    try:
        assert wait_until(lambda: sum(1 for r in mgr.snapshot() if r["recording"]) == 1, 25)
        queued = [r for r in mgr.snapshot() if r["queued"]]
        assert len(queued) == 2, queued
        for row in queued:
            assert "带宽预算" in row["queue_reason"], row["queue_reason"]
        LOG.info("带宽闸门：在录=1 排队=2 原因=%s", queued[0]["queue_reason"])
        cfg["max_total_mbps"] = 20
        assert wait_until(lambda: sum(1 for r in mgr.snapshot() if r["recording"]) == 3, 25)
        LOG.info("带宽预算放大后 3 路全部开录")
    finally:
        mgr.stop(wait=True, timeout=40)
    return True


# ---------------------------------------------------------------------------
# 状态变化才打 INFO 日志
# ---------------------------------------------------------------------------
def test_state_change_logging():
    cfg, wl, mgr, _out = make_manager("logs", item_count=0)
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler()
    logger = logging.getLogger("dyrec.manager")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        item = WatchItem(name="日志测试", id="log-item")
        mgr._apply_check(item, StatusView(is_live=True, title="直播中"))
        first = sum(1 for r in records if "开播" in r.getMessage())
        mgr._apply_check(item, StatusView(is_live=True, title="直播中"))
        second = sum(1 for r in records if "开播" in r.getMessage())
        mgr._apply_check(item, StatusView(is_live=False))
        third = sum(1 for r in records if "下播" in r.getMessage())
        LOG.info("状态变化日志：开播=%d 重复后=%d 下播=%d", first, second, third)
        assert first == 1, first
        assert second == first, "状态未变化不应重复打 INFO"
        assert third == 1, third
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        mgr._shutdown_workers()
    return True


TESTS = [
    ("配置迁移与校验", test_config_migration),
    ("令牌桶限速", test_token_bucket_rate),
    ("自适应档位/退避", test_adaptive_intervals),
    ("熔断器", test_circuit_breaker),
    ("熔断刷新cookie", test_cookie_refresh_and_breaker_trigger),
    ("闸门原因(单元)", test_gate_reasons_unit),
    ("1360批量导入线程", test_batch_import_1360_threads),
    ("固定工作池", test_poll_worker_pool),
    ("并发闸门/排队/释放", test_gate_queue_and_release),
    ("带宽闸门", test_mbps_gate_integration),
    ("状态变化日志", test_state_change_logging),
]


def main():
    failed = []
    for name, fn in TESTS:
        LOG.info("========== 开始: %s ==========", name)
        started = time.time()
        try:
            fn()
            LOG.info("========== 通过: %s (%.1fs) ==========", name, time.time() - started)
        except Exception as exc:
            LOG.exception("========== 失败: %s -> %s ==========", name, exc)
            failed.append((name, str(exc)))
    print()
    if failed:
        print("FAILED:")
        for name, exc in failed:
            print("  -", name, ":", exc)
        return 1
    print("ALL SCHEDULER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
