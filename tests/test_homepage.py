# -*- coding: utf-8 -*-
"""「跳转到抖音主页」链接推导测试。

覆盖：
  1. 条目只有直播间地址时，不能把直播间地址当主页（主页为空）；
  2. 条目有 sec_uid 时返回 https://www.douyin.com/user/<sec_uid>；
  3. 条目 url 本身是主页链接时能提取 sec_uid；
  4. parse_target_text 能从主页链接解析 sec_uid；
  5. 状态查询结果（raw.sec_uid）会写回条目。

运行：python tests/test_homepage.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.checker import StatusView
from src.config import Config
from src.manager import MonitorManager, _status_sec_uid
from src.model import WatchItem, parse_target_text
from src.storage import Watchlist

TMP = ROOT / "tests" / "tmp" / "homepage"


def test_no_live_url_as_homepage():
    item = WatchItem(name="测试", web_rid="123456",
                     url="https://live.douyin.com/123456")
    got = MonitorManager._item_homepage(item)
    assert got == "", "直播间地址不能当主页: %r" % got
    print("PASS 直播间地址不会被当成主页")


def test_sec_uid_homepage():
    item = WatchItem(name="测试", web_rid="123456",
                     url="https://live.douyin.com/123456",
                     sec_uid="MS4wLjABAAAA_demo")
    got = MonitorManager._item_homepage(item)
    assert got == "https://www.douyin.com/user/MS4wLjABAAAA_demo", got
    print("PASS sec_uid -> 主页链接")


def test_homepage_url_extract():
    item = WatchItem(name="测试", url="https://www.douyin.com/user/MS4wLjABAAAA_x")
    got = MonitorManager._item_homepage(item)
    assert got == "https://www.douyin.com/user/MS4wLjABAAAA_x", got
    print("PASS 主页链接能提取 sec_uid")


def test_parse_target_sec_uid():
    info = parse_target_text("https://www.douyin.com/user/MS4wLjABAAAA_y")
    assert info["sec_uid"] == "MS4wLjABAAAA_y", info
    print("PASS parse_target_text 解析 sec_uid")


def test_status_sec_uid_helper():
    class _Raw:
        raw = {"sec_uid": "MS4wLjABAAAA_z"}

    st = StatusView(raw_obj=_Raw())
    assert _status_sec_uid(st) == "MS4wLjABAAAA_z"
    assert _status_sec_uid(StatusView()) == ""
    print("PASS 从查询结果提取 sec_uid")


def test_apply_check_persists_sec_uid():
    TMP.mkdir(parents=True, exist_ok=True)
    cfg = Config.load(TMP / "cfg.json")
    cfg["output_dir"] = str(TMP / "out")
    cfg["use_mock_api"] = True
    wl = Watchlist(TMP / "wl.json")
    wl.clear()
    item = wl.add_from_text("123456789", name="测试主播", save=False)
    mgr = MonitorManager(cfg, wl)

    class _Raw:
        raw = {"sec_uid": "MS4wLjABAAAA_persist"}

    st = StatusView(is_live=True, room_id="123456789", anchor_name="测试主播",
                    raw_obj=_Raw())
    mgr._apply_check(item, st)
    assert item.sec_uid == "MS4wLjABAAAA_persist", item.sec_uid
    got = MonitorManager._item_homepage(item)
    assert got == "https://www.douyin.com/user/MS4wLjABAAAA_persist", got
    mgr._shutdown_workers()
    print("PASS 查询结果 sec_uid 写回条目并生成主页链接")


def main():
    tests = [
        ("直播间地址不当主页", test_no_live_url_as_homepage),
        ("sec_uid 生成主页", test_sec_uid_homepage),
        ("主页链接提取", test_homepage_url_extract),
        ("parse sec_uid", test_parse_target_sec_uid),
        ("状态提取 sec_uid", test_status_sec_uid_helper),
        ("写回条目", test_apply_check_persists_sec_uid),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failed.append((name, str(exc)))
    print()
    if failed:
        print("FAILED:")
        for n, e in failed:
            print("  -", n, ":", e)
        return 1
    print("ALL HOMEPAGE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
