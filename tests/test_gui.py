# -*- coding: utf-8 -*-
"""GUI 冒烟测试：窗口能创建、表格能刷新、能启动/停止监控（mock 源）。"""
import logging
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

from src.config import Config
from src.mock_api import configure as mock_configure
from src.storage import Watchlist

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
LOG = logging.getLogger("test.gui")

TMP = ROOT / "tests" / "tmp"
OUT = TMP / "gui_out"


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.root.update()
        time.sleep(0.05)


def main():
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    cfg = Config.load(TMP / "test_config_gui.json")
    cfg["output_dir"] = str(OUT)
    cfg["check_interval"] = 5
    cfg["use_mock_api"] = True
    cfg["ui_refresh_ms"] = 300
    cfg["output_format"] = "flv"
    wl = Watchlist(TMP / "watchlist_gui.json")
    wl.clear()
    wl.add_from_text("111111111111", name="GUI测试主播")
    mock_configure(ffmpeg_path=cfg.get_str("ffmpeg_path"), live_seconds=30)

    from src.gui import RecorderApp
    app = RecorderApp(cfg, wl)
    pump(app, 1.0)
    LOG.info("窗口已创建，表格行数: %d", len(app.tree.get_children()))
    assert len(app.tree.get_children()) == 1, "表格应有 1 行"
    row = app.tree.item(app.tree.get_children()[0])["values"]
    LOG.info("表格首行: %s", row)
    assert row[0] in ("☐", "☑"), "首列应为勾选框"
    assert row[1] == "● 未检查", "第二列应为状态"
    assert row[2] == "GUI测试主播", "第三列应为主播名"
    assert str(row[3]) == "111111111111", "第四列应为房间号"

    # 打开设置对话框并取消（验证对话框能构建）
    from src.gui import SettingsDialog, ItemDialog
    cfg2 = app.cfg
    dlg = SettingsDialog(app.root, cfg2)
    pump(app, 0.3)
    dlg.destroy()
    dlg = ItemDialog(app.root, None, "添加主播")
    pump(app, 0.3)
    dlg.destroy()

    # 开始监控（mock 直播源）
    app.on_start()
    pump(app, 12)
    rows = app.manager.snapshot()
    LOG.info("监控状态: %s", [(r["name"], r["status"], r["recording"]) for r in rows])
    assert app.manager.is_running(), "监控未运行"
    assert any(r["recording"] for r in rows), "应有房间在录制"
    files = list(OUT.rglob("*.flv"))
    LOG.info("GUI 录制文件: %s", [(f.name, f.stat().st_size) for f in files if f.exists()])
    assert files, "GUI 未产生录制文件"

    # 停止监控
    app.on_stop()
    deadline = time.time() + 40
    while time.time() < deadline and app.manager.is_running():
        pump(app, 0.5)
    LOG.info("监控已停止: %s", not app.manager.is_running())
    assert not app.manager.is_running()
    app._closing = True
    app._destroy()
    print("GUI SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())