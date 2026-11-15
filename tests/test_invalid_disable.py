# -*- coding: utf-8 -*-
"""无效直播间自动停用回归：连续 N 次无效后停用；网络错误不停用；可重新启用。"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "invalid_disable"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
from src.checker import StatusView
from src.manager import MonitorManager, classify_lookup_error
import src.gui as G


def make_mgr(limit=3):
    cfg = Config.load(TMP / "config.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["invalid_auto_disable_after"] = limit
    cfg["log_level"] = "WARNING"
    cfg.save()
    wl = Watchlist(TMP / "watchlist.json")
    wl.clear()
    wl.add_from_text("111111111111", name="坏条目")
    return cfg, wl, MonitorManager(cfg, wl)


def test_auto_disable():
    assert classify_lookup_error("无法识别的目标（支持房间号/直播间链接）") == "invalid"
    assert classify_lookup_error("房间不存在或 web_rid 非法") == "invalid"
    cfg, wl, mgr = make_mgr(3)
    item = wl.all()[0]
    st = StatusView(is_live=False, error="无法识别的目标（支持房间号/直播间链接）")
    for i in range(3):
        mgr._apply_check(item, st)
        snap_i = mgr.snapshot()[0]
        print("第 %d 次后 enabled=%s error=%s"
              % (i + 1, item.enabled, snap_i.get("error")))
    snap = mgr.snapshot()[0]
    print("after 3:", snap.get("enabled"), snap.get("error"))
    assert item.enabled is False, "连续 3 次无效后应自动停用"
    assert "已自动停用" in str(snap.get("error")), snap.get("error")
    mgr.flush()
    wl2 = Watchlist(TMP / "watchlist.json")
    assert wl2.all()[0].enabled is False, "停用状态没有持久化"
    print("PASS 连续 3 次无效自动停用并持久化")


def test_offline_not_disabled():
    cfg, wl, mgr = make_mgr(2)
    item = wl.all()[0]
    st = StatusView(is_live=False, error="enter 接口无房间数据 (status_code=0)")
    for _ in range(5):
        mgr._apply_check(item, st)
    snap = mgr.snapshot()[0]
    print("offline after 5:", snap.get("enabled"), snap.get("status"), snap.get("error"))
    assert item.enabled is True, "未开播（无房间数据）不应被停用"
    assert str(snap.get("status")) == "未开播", snap.get("status")
    print("PASS 未开播类错误不会被停用")


def test_disable_off():
    cfg, wl, mgr = make_mgr(0)   # 0 = 不自动停用
    item = wl.all()[0]
    st = StatusView(is_live=False, error="房间不存在或 web_rid 非法")
    for _ in range(6):
        mgr._apply_check(item, st)
    assert item.enabled is True, "阈值=0 时不应自动停用"
    print("PASS 阈值=0 时保持重试")


def test_gui_reenable():
    cfg = Config.load(TMP / "config.json")
    cfg["output_dir"] = str(TMP / "out2")
    cfg.save()
    wl = Watchlist(TMP / "gui_wl.json")
    wl.clear()
    item = wl.add_from_text("222222222222", name="停用主播")
    item.enabled = False
    wl.save()
    app = G.RecorderApp(cfg, wl)
    for _ in range(20):
        app.root.update(); time.sleep(0.01)
    app._refresh_table(force=True)
    app.tree.selection_set(item.id)
    app.root.update()
    app._reenable_selected()
    for _ in range(5):
        app.root.update()
    print("reenabled:", app.watchlist.get(item.id).enabled)
    assert app.watchlist.get(item.id).enabled is True
    labels = []
    try:
        end = app._ctx_menu.index("end")
        for i in range(int(end) + 1):
            try:
                labels.append(app._ctx_menu.entrycget(i, "label"))
            except Exception:
                pass
    except Exception:
        pass
    assert "重新启用并检查" in labels, labels
    app._closing = True; app._destroy()
    print("PASS 右键重新启用")


def main():
    test_auto_disable()
    test_offline_not_disabled()
    test_disable_off()
    test_gui_reenable()
    print("ALL INVALID_DISABLE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
