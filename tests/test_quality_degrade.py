# -*- coding: utf-8 -*-
"""清晰度默认最高档 + 断流自动降档测试（P1-E）。

覆盖：
  1. 默认 quality=origin、支持 quality=best 透传给 checker.choose_url；
  2. quality_ladder 配置规范化；
  3. 断流重连达到阈值 -> 自动降一档（origin -> hd），写 INFO 事件与 history，
     snapshot 输出 effective_quality/degraded 且实际重开的新 recorder 用新档位；
  4. 下播后恢复用户设定档位（effective_quality 回到 origin、degraded=False）；
  5. quality_degrade_enabled=False 时不降档；
  6. 已是最低档（sd）时不再降档。

运行：python tests/test_quality_degrade.py
"""
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 测试使用独立历史库，避免污染真实 data/history.db
os.environ["DYREC_HISTORY_DB"] = str(ROOT / "tests" / "tmp" / "quality_history.db")

from src import history
from src.checker import StatusView
from src.config import Config, DEFAULTS
from src.manager import MonitorManager
from src.mock_api import configure as mock_configure
from src.model import EventType
from src.storage import Watchlist

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
LOG = logging.getLogger("test.quality")

TMP = ROOT / "tests" / "tmp" / "quality"


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


def make_manager(name: str, item_count: int = 1, quality: str = "origin",
                 auto_record: bool = True, disconnect_after: float = 3.0, **overrides):
    """构造使用 mock HTTP-FLV 源的 Manager（断流由 mock 服务模拟）。"""
    work = reset_dir(name)
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    cfg = Config.load(work / "config.json")
    cfg["output_dir"] = str(out)
    cfg["use_mock_api"] = True
    cfg["remux_mp4"] = False
    cfg["min_keep_seconds"] = 0
    cfg["min_free_gb"] = 0
    cfg["keep_days"] = 0
    # 调度参数调小，加速测试
    cfg["poll_interval_live"] = 1
    cfg["poll_interval_warm"] = 1
    cfg["poll_interval_cold"] = 1
    cfg["poll_rate_limit"] = 50
    cfg["max_poll_workers"] = 4
    # 录制重连参数：关闭 ffmpeg 内部重连，让 recorder 自己重连（会产生 RECONNECT 事件）
    cfg["ffmpeg_http_args"] = []
    cfg["retry_max"] = 10
    cfg["retry_interval"] = 1
    cfg["retry_interval_max"] = 1
    cfg["quality_degrade_enabled"] = True
    cfg["quality_degrade_after_reconnects"] = 2
    cfg["quality_ladder"] = "origin,hd,sd"
    for key, value in overrides.items():
        cfg[key] = value
    wl = Watchlist(work / "watchlist.json")
    wl.clear()
    for index in range(item_count):
        wl.add_from_text("1%011d" % (index + 1), name="清晰度%d" % (index + 1),
                         quality=quality, auto_record=auto_record)
    mock_configure(ffmpeg_path=cfg.get_str("ffmpeg_path"), live_seconds=60,
                   offline_after=0, disconnect_after=disconnect_after)
    events = []
    mgr = MonitorManager(cfg, wl, on_event=events.append)
    return cfg, wl, mgr, events


def degrade_events(events):
    return [ev for ev in events
            if ev.kind == EventType.INFO and "清晰度降为" in (ev.message or "")]


def row_of(mgr, item_id):
    for row in mgr.snapshot():
        if row.get("id") == item_id:
            return row
    return {}


# ---------------------------------------------------------------------------
# 1) 默认档位 / best 透传 / 阶梯配置
# ---------------------------------------------------------------------------
def test_quality_defaults_and_best():
    assert DEFAULTS["quality"] == "origin"
    assert DEFAULTS["quality_degrade_enabled"] is True
    assert DEFAULTS["quality_degrade_after_reconnects"] == 2
    assert DEFAULTS["quality_ladder"] == "origin,hd,sd"

    # 配置文件里的非法/重复阶梯会被规范化
    work = reset_dir("cfg")
    path = work / "cfg.json"
    data = dict(DEFAULTS)
    data["quality"] = "best"
    data["quality_ladder"] = " ORIGIN, hd,,bad, sd,hd "
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = Config.load(path)
    LOG.info("配置规范化: quality=%s ladder=%s", cfg["quality"], cfg["quality_ladder"])
    assert cfg["quality"] == "best"
    assert cfg["quality_ladder"] == "origin,hd,sd"

    # 管理端：user_quality / 阶梯 / best 透传
    cfg, wl, mgr, _events = make_manager("best", item_count=1)
    try:
        item = wl.all()[0]
        item.quality = ""
        cfg["quality"] = "best"
        assert mgr._user_quality(item) == "best"
        # 全局 best 作为默认最高档：房间条目仍是默认 origin（等价于未指定）时也走 best
        item.quality = "origin"
        assert mgr._user_quality(item) == "best"
        item.quality = "hd"
        assert mgr._user_quality(item) == "hd"
        item.quality = "origin"
        assert mgr._ladder_qualities() == ["origin", "hd", "sd"]
        assert mgr._next_degrade_quality("origin") == "hd"
        assert mgr._next_degrade_quality("hd") == "sd"
        assert mgr._next_degrade_quality("sd") is None
        assert mgr._next_degrade_quality("best") == "hd"
        captured = []
        mgr.checker.choose_url = lambda st, q: captured.append(q) or None
        ok = mgr._start_recording(item, StatusView(is_live=True), quality="best")
        LOG.info("best 透传: ok=%s captured=%s", ok, captured)
        assert ok is False   # 假 choose_url 返回 None，验证的是传参
        assert captured == ["best"], captured
        # 不传 quality 时使用用户设定（此处配置默认 best，item 为默认 origin）
        captured.clear()
        mgr._start_recording(item, StatusView(is_live=True))
        assert captured and captured[0] == "best", captured
    finally:
        mgr._shutdown_workers()
    return True


# ---------------------------------------------------------------------------
# 2) 断流降档 + 下播恢复
# ---------------------------------------------------------------------------
def test_auto_degrade_and_recover():
    cfg, wl, mgr, events = make_manager("degrade", disconnect_after=3.0)
    item = wl.all()[0]
    ok, msg = mgr.start()
    assert ok, msg
    try:
        # 等第一次降档：effective_quality 变为 hd 且 degraded=True
        changed = wait_until(
            lambda: row_of(mgr, item.id).get("effective_quality") == "hd"
            and row_of(mgr, item.id).get("degraded") is True,
            50,
        )
        snap = row_of(mgr, item.id)
        LOG.info("降档后快照: effective=%s degraded=%s status=%s",
                 snap.get("effective_quality"), snap.get("degraded"), snap.get("status"))
        assert changed, "未触发自动降档"
        assert snap.get("quality") == "origin", "用户设定档位不应被改写"
        infos = degrade_events(events)
        assert infos, "缺少降档 INFO 事件: %s" % [ev.message for ev in events
                                                 if ev.kind == EventType.INFO]
        message = infos[0].message
        LOG.info("降档事件: %s", message)
        assert "连续断流" in message and "hd" in message

        # history 落库（source=manager:quality）
        errors = history.list_recent_errors(days=1)
        assert any("manager:quality" == (row.get("source") or "")
                   and "清晰度降为 hd" in (row.get("message") or "")
                   for row in errors), errors[-3:]

        # 实际重开的新 recorder 使用 hd 档位
        relaunched = wait_until(
            lambda: any(getattr(rec, "quality", "") == "hd"
                        for rec in list(mgr._recorders.values())),
            40,
        )
        LOG.info("重开 recorder 使用 hd: %s", relaunched)
        assert relaunched, "降档后没有按 hd 重开录制"

        # 下播：恢复用户设定档位
        mgr._apply_check(item, StatusView(is_live=False))
        snap = row_of(mgr, item.id)
        LOG.info("下播后快照: effective=%s degraded=%s",
                 snap.get("effective_quality"), snap.get("degraded"))
        assert snap.get("effective_quality") == "origin"
        assert snap.get("degraded") is False
        assert mgr._session_quality.get(item.id) is None
        assert mgr._session_reconnects.get(item.id) is None
    finally:
        mgr.stop(wait=True, timeout=40)
    return True


# ---------------------------------------------------------------------------
# 3) 关闭开关不降档
# ---------------------------------------------------------------------------
def test_degrade_disabled():
    cfg, wl, mgr, events = make_manager("disabled", disconnect_after=2.0,
                                        quality_degrade_enabled=False)
    item = wl.all()[0]
    ok, msg = mgr.start()
    assert ok, msg
    try:
        # 确认确实发生了断流重连（内部计数 >= 2）
        counted = wait_until(
            lambda: mgr._session_reconnects.get(item.id, 0) >= 2, 30)
        LOG.info("关闭降档时本场重连次数=%s", mgr._session_reconnects.get(item.id))
        assert counted, "没有发生断流重连，无法验证开关"
        snap = row_of(mgr, item.id)
        assert snap.get("effective_quality") == "origin", snap
        assert snap.get("degraded") is False
        assert not degrade_events(events), [ev.message for ev in degrade_events(events)]
    finally:
        mgr.stop(wait=True, timeout=40)
    return True


# ---------------------------------------------------------------------------
# 4) 已是最高/最低档时不再降档
# ---------------------------------------------------------------------------
def test_lowest_quality_no_degrade():
    cfg, wl, mgr, events = make_manager("lowest", disconnect_after=2.0, quality="sd")
    item = wl.all()[0]
    ok, msg = mgr.start()
    assert ok, msg
    try:
        counted = wait_until(
            lambda: mgr._session_reconnects.get(item.id, 0) >= 2, 30)
        assert counted, "没有发生断流重连"
        snap = row_of(mgr, item.id)
        LOG.info("最低档断流后: effective=%s degraded=%s",
                 snap.get("effective_quality"), snap.get("degraded"))
        assert snap.get("effective_quality") == "sd"
        assert snap.get("degraded") is False
        assert not degrade_events(events), [ev.message for ev in degrade_events(events)]
    finally:
        mgr.stop(wait=True, timeout=40)
    return True


# ---------------------------------------------------------------------------
# 5) 手动录制（auto_record=False）也适用降档并自动重开
# ---------------------------------------------------------------------------
def test_manual_record_degrades():
    cfg, wl, mgr, events = make_manager("manual", auto_record=False,
                                        disconnect_after=2.0)
    item = wl.all()[0]
    ok, msg = mgr.start()
    assert ok, msg
    try:
        started, message = mgr.manual_record(item.id)
        LOG.info("手动录制: %s %s", started, message)
        assert started, message
        changed = wait_until(
            lambda: row_of(mgr, item.id).get("effective_quality") == "hd", 45)
        assert changed, "手动录制断流后未触发降档"
        assert item.id in mgr._manual_sessions, "手动会话标记丢失"
        relaunched = wait_until(
            lambda: any(getattr(rec, "quality", "") == "hd"
                        for rec in list(mgr._recorders.values())),
            40,
        )
        LOG.info("手动录制降档后按 hd 重开: %s", relaunched)
        assert relaunched, "手动录制降档后未自动重开"
        # 手动停止后清理本场状态
        stopped, stop_message = mgr.manual_stop(item.id)
        assert stopped, stop_message
        cleaned = wait_until(lambda: item.id not in mgr._manual_sessions, 20)
        assert cleaned, "手动停止后降档状态未清理"
        assert row_of(mgr, item.id).get("effective_quality") == "origin"
    finally:
        mgr.stop(wait=True, timeout=40)
    return True


TESTS = [
    ("默认档位/best透传", test_quality_defaults_and_best),
    ("断流自动降档+恢复", test_auto_degrade_and_recover),
    ("关闭开关不降档", test_degrade_disabled),
    ("最低档不再降档", test_lowest_quality_no_degrade),
    ("手动录制也降档", test_manual_record_degrades),
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
    history.close_all()
    print()
    if failed:
        print("FAILED:")
        for name, exc in failed:
            print("  -", name, ":", exc)
        return 1
    print("ALL QUALITY DEGRADE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
