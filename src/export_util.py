# -*- coding: utf-8 -*-
"""主播列表导出：把当前 watchlist 完整导出为 xlsx / csv，供升级后无缝导入。

导出列与 import_util.records_from_table 的识别规则对应：
  序号 / 主播名称 / 房间号 / 主页链接 / 直播间链接 / 原始目标 /
  分组 / 画质 / 自动录制 / 参与监控 / 输出模板 / 备注
"""
from __future__ import annotations

import csv
import os
import re
from typing import List, Sequence

from . import xlsx_writer
from .model import parse_target_text

__all__ = ["EXPORT_HEADERS", "watchlist_rows", "export_watchlist"]

EXPORT_HEADERS = ["序号", "主播名称", "房间号", "主页链接", "直播间链接", "原始目标",
                  "分组", "画质", "自动录制", "参与监控", "输出模板", "备注"]


def _homepage_of(item) -> str:
    for raw in (getattr(item, "url", "") or "", getattr(item, "target", "") or ""):
        m = re.search(r"douyin\.com/user/([A-Za-z0-9_\-]+)", raw or "")
        if m:
            return "https://www.douyin.com/user/" + m.group(1)
    url = (getattr(item, "url", "") or "").strip()
    if url.startswith("http") and "douyin.com" in url and "/user/" not in url:
        return url
    return ""


def _live_url_of(item) -> str:
    return ("https://live.douyin.com/%s" % item.web_rid) if getattr(item, "web_rid", "") else ""


def _yes(flag: bool) -> str:
    return "是" if flag else "否"


def _is_name_only(item) -> bool:
    """没有房间号/主页链接、只有昵称的条目（无法监控，导出时标记停用）。"""
    if getattr(item, "web_rid", "") or _homepage_of(item) or _live_url_of(item):
        return False
    info = parse_target_text(getattr(item, "target", "") or "")
    return not info.get("web_rid") and not info.get("url")


def watchlist_rows(watchlist) -> List[List[str]]:
    rows: List[List[str]] = [list(EXPORT_HEADERS)]
    for idx, item in enumerate(watchlist.all(), start=1):
        rows.append([
            str(idx),
            item.name or "",
            item.web_rid or "",
            _homepage_of(item),
            _live_url_of(item),
            item.target or "",
            getattr(item, "group", "") or "",
            item.quality or "origin",
            _yes(bool(item.auto_record)),
            _yes(bool(item.enabled) and not _is_name_only(item)),
            item.output_template or "",
            item.note or "",
        ])
    return rows


def export_watchlist(watchlist, path: str) -> int:
    """导出全部主播。按扩展名选择 xlsx / csv（其余按 xlsx）。返回条数。"""
    rows = watchlist_rows(watchlist)
    out = str(path)
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    ext = os.path.splitext(out)[1].lower()
    if ext in (".csv", ".txt"):
        with open(out, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerows(rows)
    else:
        if not ext:
            out += ".xlsx"
        xlsx_writer.write_xlsx(out, rows)
    return len(rows) - 1
