# -*- coding: utf-8 -*-
"""主播列表导入工具：从 Excel/CSV/文本解析出 (名称, 目标)，去重并统计。

去重规则（按优先级）：
  1) 同一 web_rid（房间号）视为重复；
  2) 同一主播主页 sec_uid 视为重复；
  3) 同一规范化 URL（去 query/fragment）视为重复；
  4) 纯文本输入按小写文本视为重复。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .model import parse_target_text
from .xlsx_reader import read_csv, read_table, read_table_sheets, read_xlsx  # noqa: F401  (对外统一入口)

URL_RE = re.compile(r"https?://[^\s\"'<>，。；）)】]+")
# 表头识别用「精确 token」：避免把「主播丙」这类昵称误判成表头
_HEADER_TOKENS = (
    "昵称", "主播名称", "主播名", "名称", "名字", "账号", "主播",
    "房间号", "房间", "roomid", "room",
    "主页链接", "主页", "homepage", "直播间链接", "直播间地址", "直播间", "liveurl",
    "链接", "地址", "url", "link",
    "备注", "分组", "group", "画质", "清晰度", "quality",
    "自动录制", "auto", "参与监控", "启用", "输出模板", "目录模板", "模板", "template",
    "原始目标", "原始输入", "target", "note",
)


def normalize_url(url: str) -> str:
    u = (url or "").strip().rstrip("，。；;,)）]】")
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    # 去掉 query / fragment（分享短链除外，短链 query 一般无意义）
    u = u.split("#", 1)[0].split("?", 1)[0]
    m = re.match(r"https://(?:www\.)?(douyin\.com|live\.douyin\.com|v\.douyin\.com)/(.*)$", u, re.I)
    if m:
        u = "https://%s/%s" % (m.group(1).lower(), m.group(2).rstrip("/"))
    return u.lower()


def target_key(text: str) -> str:
    """把一条输入归一为去重键。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    info = parse_target_text(raw)
    rid = (info.get("web_rid") or "").strip()
    if rid:
        return "rid:" + rid
    url = (info.get("url") or "").strip() or (URL_RE.search(raw).group(0) if URL_RE.search(raw) else "")
    if url:
        n = normalize_url(url)
        m = re.search(r"douyin\.com/user/([A-Za-z0-9_\-]+)", n)
        if m:
            return "sec:" + m.group(1)
        return "url:" + n
    return "txt:" + re.sub(r"\s+", " ", raw).lower()


def _norm_header(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


_FIELD_HINTS = {
    "name": ("主播名称", "主播名", "主播", "账号", "昵称", "名字", "name"),
    "web_rid": ("房间号", "roomid", "room"),
    "homepage": ("主页链接", "主页", "homepage"),
    "live_url": ("直播间链接", "直播间地址", "直播间", "liveurl"),
    "target": ("原始目标", "原始输入", "target"),
    "group": ("分组", "group"),
    "quality": ("画质", "清晰度", "quality"),
    "auto_record": ("自动录制", "auto"),
    "enabled": ("参与监控", "启用", "监控"),
    "output_template": ("输出模板", "目录模板", "模板", "template"),
    "note": ("备注", "note"),
}


def _map_columns(header: Sequence[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i, cell in enumerate(header):
        norm = _norm_header(cell)
        if not norm:
            continue
        for field, hints in _FIELD_HINTS.items():
            if field in out:
                continue
            if any(h in norm for h in hints):
                out[field] = i
                break
    return out


def _is_header_row(row: Sequence[str]) -> bool:
    """判断一行是否像表头：包含已知表头词，且本身不是 URL/房间号数据行。"""
    cells = [str(c).strip() for c in row if str(c).strip()]
    if not cells:
        return False
    for c in cells:
        if URL_RE.search(c):
            return False
        if c.isdigit() and len(c) >= 5:
            return False
    for c in cells:
        norm = _norm_header(c)
        if not norm:
            continue
        for token in _HEADER_TOKENS:
            if norm == token:
                return True
            if norm.startswith(token) and len(norm) <= len(token) + 8:
                return True
    return False


def records_from_table(rows: Sequence[Sequence[str]]) -> Tuple[List[Dict[str, str]], int]:
    """从表格行解析主播记录。

    兼容两种格式：
      1) 简单两列（账号/链接或房间号）；
      2) 本工具导出的完整列（主播名称/房间号/主页链接/直播间链接/分组/画质/自动录制/参与监控/输出模板/备注）。
    返回 ([{name,target,web_rid,homepage,group,quality,auto_record,enabled,output_template,note}], 无效行数)。
    """
    records: List[Dict[str, str]] = []
    skipped = 0
    if not rows:
        return records, skipped
    start = 0
    header = None
    first = [str(c).strip() for c in rows[0]]
    if _is_header_row(first):
        header = first
        start = 1
    colmap = _map_columns(header) if header else {}

    def cell(cells: List[str], field: str) -> str:
        idx = colmap.get(field)
        if idx is None or idx >= len(cells):
            return ""
        return cells[idx]

    def find_url(text: str) -> str:
        m = URL_RE.search(text or "")
        return m.group(0) if m else ""

    for row in rows[start:]:
        cells = [str(c).strip() for c in row]
        if not any(cells):
            continue
        web_rid = cell(cells, "web_rid").strip()
        if not (web_rid.isdigit() and len(web_rid) >= 5):
            web_rid = ""
        homepage = find_url(cell(cells, "homepage"))
        live_url = find_url(cell(cells, "live_url"))
        raw_target = cell(cells, "target").strip()
        target = web_rid or live_url or homepage or (raw_target if find_url(raw_target) else "")
        if not target:
            for c in cells:
                u = find_url(c)
                if u:
                    target = u
                    break
        if not target:
            for c in cells:
                if c.isdigit() and len(c) >= 5:
                    target = c
                    break
        name = cell(cells, "name")
        if not name and target:
            for c in cells:
                if not c or find_url(c) or (c.isdigit() and len(c) >= 5):
                    continue
                if len(c) > len(name):
                    name = c
        enabled_text = cell(cells, "enabled").strip().lower()
        name_only_disabled = (not target and bool(raw_target) and
                              enabled_text in ("否", "no", "false", "0", "关闭", "禁用"))
        if not target and name_only_disabled:
            target = raw_target
            if not name:
                name = raw_target
        if not target:
            skipped += 1
            continue
        rec: Dict[str, str] = {"name": name[:80], "target": target}
        for field in ("group", "quality", "auto_record", "enabled",
                      "output_template", "note"):
            value = cell(cells, field)
            if value:
                rec[field] = value
        if web_rid:
            rec["web_rid"] = web_rid
        if homepage:
            rec["homepage"] = homepage
        if live_url:
            rec["live_url"] = live_url
        records.append(rec)
    return records, skipped


def records_from_sheets(sheets) -> Tuple[List[Dict[str, str]], int, List[Dict[str, object]]]:
    """逐个 sheet 解析主播记录（每个 sheet 独立识别表头），返回 (记录, 无效数, 明细)。

    明细：[{sheet, rows, records, skipped}]，用于导入结果提示「读取了几个 sheet」。
    """
    records: List[Dict[str, str]] = []
    skipped = 0
    details: List[Dict[str, object]] = []
    for name, rows in sheets:
        recs, sk = records_from_table(rows)
        details.append({"sheet": str(name or ""), "rows": len(rows),
                        "records": len(recs), "skipped": sk})
        records.extend(recs)
        skipped += sk
    return records, skipped, details


def records_from_file(path: str) -> Tuple[List[Dict[str, str]], int, List[Dict[str, object]]]:
    """读取 Excel/CSV 文件：xlsx 会读取全部工作表并按 sheet 分别识别表头。"""
    sheets = read_table_sheets(path)
    return records_from_sheets(sheets)


def records_from_lines(lines: Iterable[str]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for line in lines:
        text = (line or "").strip()
        if not text:
            continue
        m = URL_RE.search(text)
        target = m.group(0) if m else text
        name = ""
        if m and m.start() > 0:
            name = text[:m.start()].strip(" ，,。-—:：|[]【】")
        out.append({"name": name[:80], "target": target})
    return out


def dedupe(records: Iterable[Dict[str, str]], existing_keys: Optional[Set[str]] = None
           ) -> Tuple[List[Dict[str, str]], int, int]:
    """去重。返回 (去重后记录, 重复数, 无效数)。existing_keys 为已有列表的去重键集合。"""
    seen: Set[str] = set(existing_keys or set())
    accepted: List[Dict[str, str]] = []
    dup = 0
    invalid = 0
    for rec in records:
        target = (rec.get("target") or "").strip()
        if not target:
            invalid += 1
            continue
        key = target_key(target)
        if not key:
            invalid += 1
            continue
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        clean = dict(rec)
        clean["name"] = (rec.get("name") or "").strip()[:80]
        clean["target"] = target
        accepted.append(clean)
    return accepted, dup, invalid

_TRUE_TEXT = ("是", "yes", "true", "1", "开", "开启", "启用", "y", "on")
_FALSE_TEXT = ("否", "no", "false", "0", "关", "关闭", "禁用", "n", "off")


def parse_bool(value, default: bool = True) -> bool:
    """把导出/手工表格里的「是/否/true/false」转成 bool。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in _TRUE_TEXT:
        return True
    if text in _FALSE_TEXT:
        return False
    return default


_QUALITY_TEXT = {
    "origin": "origin", "原画": "origin", "原画优先": "origin", "跟随最高": "origin",
    "best": "best", "自动": "best", "自动最高": "best",
    "hd": "hd", "高清": "hd", "高清 720p": "hd", "720p": "hd",
    "sd": "sd", "标清": "sd", "标清 540p": "sd", "540p": "sd",
}


def parse_quality(value, default: str = "origin") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in _QUALITY_TEXT:
        return _QUALITY_TEXT[text]
    for key, quality in _QUALITY_TEXT.items():
        if key and key in text:
            return quality
    return default
