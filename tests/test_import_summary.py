# -*- coding: utf-8 -*-
"""导入结果统计测试：成功 / 已存在（重复）/ 失败 数量要显示。"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "import_summary"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
import src.gui as G


class FakeDialog:
    def __init__(self, parent, result):
        self.result = result


def make_app():
    cfg = Config.load(TMP / "config.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["log_level"] = "WARNING"
    cfg.save()
    wl = Watchlist(TMP / "watchlist.json")
    wl.clear()
    app = G.RecorderApp(cfg, wl)
    for _ in range(30):
        app.root.update(); time.sleep(0.01)
    return app, wl


def run_import(app, result):
    captured = {}
    orig_dialog = G.BatchImportDialog
    orig_info = G.messagebox.showinfo
    orig_wait = app.root.wait_window
    G.BatchImportDialog = lambda parent: FakeDialog(parent, result)
    G.messagebox.showinfo = lambda title, msg, **kw: captured.setdefault("msg", msg)
    app.root.wait_window = lambda dlg=None: None
    try:
        app._batch_import()
    finally:
        G.BatchImportDialog = orig_dialog
        G.messagebox.showinfo = orig_info
        app.root.wait_window = orig_wait
    return captured.get("msg", "")


def main():
    app, wl = make_app()
    result = {
        "records": [
            {"name": "主播甲", "target": "111111111111"},
            {"name": "主播乙", "target": "https://www.douyin.com/user/MS4wLjABAAAA_demo_x"},
        ],
        "lines": [],
        "quality": "origin",
        "auto_record": True,
        "group": "",
        "dup": 0,
        "invalid": 1,
        "file_skipped": 1,
    }
    msg1 = run_import(app, result)
    print("第一次导入结果:\n" + msg1)
    assert "成功导入：2 个" in msg1, msg1
    assert "已存在（重复跳过）：0 个" in msg1
    assert "失败（无法识别/缺链接）：1 个" in msg1

    msg2 = run_import(app, result)
    print("\n第二次导入（重复）结果:\n" + msg2)
    assert "成功导入：0 个" in msg2, msg2
    assert "已存在（重复跳过）：2 个" in msg2
    assert "失败（无法识别/缺链接）：1 个" in msg2

    # 导入对话框的实时统计文案
    dlg = G.BatchImportDialog(app.root)
    dlg.text.insert("1.0", "111111111111\n222222222222\n")
    dlg._update_count()
    app.root.update()
    label = dlg.count_label.cget("text")
    print("\n对话框实时统计:", label)
    assert "将导入 2 个" in label and "已存在（重复）" in label and "无法识别" in label
    dlg.destroy()
    for _ in range(5):
        app.root.update()
    app._closing = True
    app._destroy()
    print("ALL IMPORT SUMMARY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
