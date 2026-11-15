# -*- coding: utf-8 -*-
"""主播活跃度：last_live_at 持久化 / 统计 / GUI 列与窗口 / CLI。"""
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "activity_test"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
from src.model import WatchItem
from src import activity, mock_api
from src.manager import MonitorManager
import src.gui as G


def test_format_ago():
    now = time.time()
    assert activity.format_ago(0) == "从未"
    assert activity.format_ago(int(now) - 30) == "刚刚"
    assert "分钟前" in activity.format_ago(int(now) - 600)
    assert "小时前" in activity.format_ago(int(now) - 3600 * 5)
    assert "天前" in activity.format_ago(int(now) - 86400 * 3)
    print("PASS format_ago")


def test_collect_and_csv():
    wl = Watchlist(TMP / "activity_wl.json")
    wl.clear()
    now = time.time()
    a = wl.add_from_text("111111111111", name="三天前", save=False)
    a.last_live_at = int(now - 86400 * 3)
    b = wl.add_from_text("222222222222", name="从未开播", save=False)
    c = wl.add_from_text("333333333333", name="录像回填", save=False)
    wl.save()
    # 给 c 造一个录像目录（按模板 {anchor}/{date}）
    cfg = Config.load(TMP / "activity_config.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg.save()
    d = Path(cfg.output_root) / "录像回填" / "2026-11-01"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "录像回填_2026-11-01_120000.flv"
    f.write_bytes(b"x")
    old = time.time() - 86400 * 12
    os.utime(f, (old, old))
    rows = activity.collect_activity(wl, cfg.output_root, cfg.get_str("output_template"),
                                     backfill=True)
    print("rows:", [(r["name"], r["ago"], r["source"]) for r in rows])
    assert len(rows) == 3
    assert rows[0]["name"] == "从未开播" and rows[0]["ago"] == "从未"
    by = {r["name"]: r for r in rows}
    assert by["三天前"]["source"] == "recorded"
    assert by["录像回填"]["source"] == "files" and by["录像回填"]["last_live_at"] > 0
    out = TMP / "activity.csv"
    n = activity.write_csv(rows, str(out))
    assert n == 3
    content = out.read_text(encoding="utf-8-sig")
    assert "主播" in content and "从未开播" in content
    print("PASS collect_activity + write_csv")


def test_manager_persists_last_live():
    cfg = Config.load(TMP / "mgr_config.json")
    cfg["output_dir"] = str(TMP / "mgr_out")
    cfg["use_mock_api"] = True
    cfg["check_interval"] = 3
    cfg["poll_interval_live"] = 3
    cfg["log_level"] = "WARNING"
    cfg.save()
    mock_api.configure(ffmpeg_path=str(ROOT / "bin" / "ffmpeg.exe"), live_seconds=60)
    wl = Watchlist(TMP / "mgr_wl.json")
    wl.clear()
    wl.add_from_text("111111111111", name="活跃测试")
    mgr = MonitorManager(cfg, wl)
    ok, msg = mgr.start()
    assert ok, msg
    item = wl.all()[0]
    deadline = time.time() + 20
    while time.time() < deadline and not item.last_live_at:
        time.sleep(0.3)
    snap = mgr.snapshot()[0]
    print("last_live_at:", item.last_live_at, "snapshot:", snap.get("last_live_at"))
    assert item.last_live_at > 0, "开播后没有记录 last_live_at"
    assert int(snap.get("last_live_at") or 0) > 0
    mgr.flush()
    mgr.stop(wait=True, timeout=60)
    wl2 = Watchlist(TMP / "mgr_wl.json")
    saved = wl2.all()[0]
    print("persisted:", saved.last_live_at)
    assert saved.last_live_at > 0, "last_live_at 没有持久化"
    print("PASS 管理端记录并持久化 last_live_at")


def test_gui_and_cli():
    cfg = Config.load(TMP / "activity_config.json")
    wl = Watchlist(TMP / "activity_wl.json")
    app = G.RecorderApp(cfg, wl)
    for _ in range(20):
        app.root.update(); time.sleep(0.01)
    app._refresh_table(force=True)
    app.root.update()
    rows = app.tree.get_children()
    vals = [app.tree.item(r)["values"] for r in rows]
    print("gui rows:", vals)
    # 表格中应有「最近开播」列（第 5 列）
    cols = app.tree.cget("columns")
    assert "last_live" in cols
    idx = list(cols).index("last_live")
    assert any(str(v[idx]) != "" for v in vals)
    # 详情有「最近开播」字段
    assert "最近开播" in app._detail_rows
    # 活跃度窗口
    act_rows = activity.collect_activity(wl, cfg.output_root,
                                        cfg.get_str("output_template"))
    dlg = G.ActivityDialog(app.root, act_rows)
    for _ in range(10):
        app.root.update(); time.sleep(0.02)
    tree = None
    for child in dlg.winfo_children():
        pass
    # 找窗口里的 Treeview
    def find_tree(w):
        for c in w.winfo_children():
            if c.winfo_class() == "Treeview":
                return c
            r = find_tree(c)
            if r:
                return r
        return None
    tree = find_tree(dlg)
    assert tree is not None and len(tree.get_children()) == 3
    dlg.destroy(); app.root.update()
    app._closing = True; app._destroy()

    # CLI
    cfg_path = TMP / "cli_cfg.json"
    cfg_path.write_text(json.dumps({"output_dir": str(cfg.output_root)}), encoding="utf-8")
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
    out = TMP / "cli_activity.csv"
    r = subprocess.run([sys.executable, str(ROOT / "run.py"), "--config", str(cfg_path),
                        "--watchlist", str(TMP / "activity_wl.json"),
                        "--activity-report", str(out)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=120)
    print("cli:", r.stdout.strip()[:300])
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    assert "主播活跃度" in r.stdout and out.exists()
    print("PASS GUI 列/详情/活跃度窗口 + CLI")


def main():
    test_format_ago()
    test_collect_and_csv()
    test_manager_persists_last_live()
    test_gui_and_cli()
    print("ALL ACTIVITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
