# -*- coding: utf-8 -*-
"""主播列表去分页回归：全部主播一次性渲染、可全选、滚动条可用。"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / "tmp" / "no_pagination"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

from src.config import Config
from src.storage import Watchlist
import src.gui as G

N = 600


def main():
    cfg = Config.load(TMP / "config.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["log_level"] = "WARNING"
    cfg.save()
    wl = Watchlist(TMP / "watchlist.json")
    wl.clear()
    for i in range(N):
        wl.add_from_text(str(100000000000 + i), name="主播%04d" % i, save=False)
    wl.save()
    app = G.RecorderApp(cfg, wl)
    app.root.geometry("1255x853")
    for _ in range(30):
        app.root.update(); time.sleep(0.01)

    t0 = time.perf_counter()
    app._refresh_table(force=True)
    app.root.update()
    cost = (time.perf_counter() - t0) * 1000
    rows = app.tree.get_children()
    print("rendered rows:", len(rows), "cost: %.1f ms" % cost)
    assert len(rows) == N, "应一次性渲染全部 %d 行，实际 %d" % (N, len(rows))
    assert cost < 1500, "600 行渲染过慢: %.1f ms" % cost

    # 分页控件已移除
    for name in ("_pager_prev", "_pager_next", "_pager_label"):
        assert not hasattr(app, name), "%s 仍存在" % name

    # 全选（表头/菜单）应勾选全部
    app._select_all_page()
    app.root.update()
    assert len(app._checked) == N, "全选只勾选 %d/%d" % (len(app._checked), N)
    info = app._list_info.cget("text")
    print("footer:", info)
    assert ("已勾选 %d 个" % N) in info

    # 滚动条存在且可滚动
    scrollbars = [w for w in app.tree.master.winfo_children()
                  if w.winfo_class() == "TScrollbar"]
    print("scrollbars:", len(scrollbars))
    assert len(scrollbars) >= 2
    app.tree.yview_moveto(1.0)
    app.root.update()
    first, last = app.tree.yview()
    assert last > 0.95, (first, last)

    # 清空勾选
    app._clear_checked()
    app.root.update()
    assert not app._checked

    # 筛选后全选只勾选筛选结果
    app._search_var.set("主播0001")
    for _ in range(10):
        app.root.update(); time.sleep(0.02)
    app._select_all_page()
    app.root.update()
    print("filtered checked:", len(app._checked))
    assert len(app._checked) > 0
    assert len(app._checked) < N

    print("PASS 无分页 + 全选全部 + 滚动条")
    app._closing = True
    app._destroy()
    print("ALL NO_PAGINATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
