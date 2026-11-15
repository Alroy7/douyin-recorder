# -*- coding: utf-8 -*-
"""命令行：从 Excel/CSV/文本导入主播列表（自动去重），供脚本/批量准备使用。

用法：
  python tools/import_watchlist.py 主播列表.xlsx
  python tools/import_watchlist.py 列表.csv --watchlist watchlist.json --group 测试 --dry-run
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import import_util  # noqa: E402
from src.storage import Watchlist  # noqa: E402


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    src_path = Path(argv[1])
    watch_path = ROOT / "watchlist.json"
    group = ""
    dry = False
    quality = "origin"
    i = 2
    while i < len(argv):
        arg = argv[i]
        if arg == "--watchlist" and i + 1 < len(argv):
            watch_path = Path(argv[i + 1]); i += 2
        elif arg == "--group" and i + 1 < len(argv):
            group = argv[i + 1]; i += 2
        elif arg == "--quality" and i + 1 < len(argv):
            quality = argv[i + 1]; i += 2
        elif arg == "--dry-run":
            dry = True; i += 1
        else:
            print("未知参数:", arg)
            return 2
    if not src_path.exists():
        print("文件不存在:", src_path)
        return 2
    rows = import_util.read_table(str(src_path))
    records, skipped = import_util.records_from_table(rows)
    wl = Watchlist(watch_path)
    wl.load()
    existing = set()
    for it in wl.all():
        key = import_util.target_key(it.web_rid or it.url or it.target)
        if key:
            existing.add(key)
    accepted, dup, invalid = import_util.dedupe(records, existing)
    print("文件行数: %d  解析有效: %d  文件内重复: %d" % (len(rows), len(records), dup))
    print("无效行: %d  与现有列表重复: %d  将新增: %d" % (skipped + invalid, dup, len(accepted)))
    if dry:
        for rec in accepted[:10]:
            print("  +", rec["name"][:30], rec["target"][:80])
        print("（--dry-run，不写入）")
        return 0
    for rec in accepted:
        item = wl.add_from_text(rec["target"], name=rec.get("name") or "",
                                auto_record=True, quality=quality,
                                output_template="", save=False)
        if group:
            item.group = group
    wl.save()
    print("已写入:", watch_path, "当前共", len(wl), "个主播")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
