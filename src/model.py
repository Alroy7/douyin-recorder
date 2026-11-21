# -*- coding: utf-8 -*-
"""数据模型：主播条目、运行时状态、录制事件、文件名模板渲染。

本模块不做任何 IO，纯数据结构 + 纯函数，便于单元测试。
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Windows 文件名非法字符
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: 连续空白
_SPACES = re.compile(r"\s+")

#: 各画质别名归一化
_QUALITY_ALIAS = {
    "origin": "origin", "原画": "origin", "原画质": "origin", "蓝光": "origin",
    "full_hd": "hd", "hd": "hd", "高清": "hd", "超清": "hd",
    "sd": "sd", "标清": "sd", "流畅": "sd", "low": "sd",
}


def sanitize_filename(text: str, maxlen: int = 80, default: str = "未命名") -> str:
    """清理成合法文件名/目录名（去非法字符、去首尾空白和点、限长）。"""
    s = (text or "").strip()
    if not s:
        s = default
    s = _ILLEGAL_FS.sub("_", s)
    s = _SPACES.sub(" ", s).strip(" .")
    if not s:
        s = default
    # 避免 Windows 保留名
    if s.upper().split(".")[0] in (
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    ):
        s = "_" + s
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(" .")
    return s or default


def normalize_quality(value: str, default: str = "origin") -> str:
    """把「原画/高清/标清」等写法归一成 origin/hd/sd。"""
    key = (value or "").strip().lower()
    return _QUALITY_ALIAS.get(key, default)


# --------------------------------------------------------------------------
# 目标（链接/房间号/分享文案）解析
# --------------------------------------------------------------------------
_RE_ROOM = re.compile(r"(?:live\.douyin\.com/|/root/)(\d{5,})")
_RE_LIVE_URL = re.compile(r"https?://[^\s\"']*live\.douyin\.com[^\s\"']*")
_RE_ANY_URL = re.compile(r"https?://[^\s\"']+")
_RE_DIGITS = re.compile(r"^\s*(\d{5,})\s*$")
_RE_USER = re.compile(r"douyin\.com/user/([A-Za-z0-9_\-]+)")


def parse_target_text(text: str) -> Dict[str, Optional[str]]:
    """本地（不联网）解析用户输入，返回 {web_rid, url, short_url, text}。

    支持：
      - 纯数字房间号：123456789
      - live.douyin.com/123456789 完整链接
      - 分享文案：复制打开抖音...【XX的直播间】... https://v.douyin.com/xxxx/
    短链（v.douyin.com）无法本地解析出房间号，交给 dy_api.resolve_target 联网跟进。
    """
    raw = (text or "").strip()
    info: Dict[str, Optional[str]] = {
        "web_rid": None, "sec_uid": None, "url": "", "short_url": "", "text": raw,
    }
    if not raw:
        return info

    m = _RE_DIGITS.match(raw)
    if m:
        info["web_rid"] = m.group(1)
        info["url"] = f"https://live.douyin.com/{m.group(1)}"
        return info

    m = _RE_USER.search(raw)
    if m:  # 电脑主播主页链接 https://www.douyin.com/user/<sec_uid>
        info["sec_uid"] = m.group(1)

    m = _RE_ROOM.search(raw)
    if m:
        info["web_rid"] = m.group(1)

    m = _RE_LIVE_URL.search(raw)
    if m:
        info["url"] = m.group(0).rstrip("，。,.;；)）]】")
    else:
        m = _RE_ANY_URL.search(raw)
        if m:
            url = m.group(0).rstrip("，。,.;；)）]】")
            info["url"] = url
            if "v.douyin.com" in url or "b23" in url:
                info["short_url"] = url
    if info["web_rid"] and not info["url"]:
        info["url"] = f"https://live.douyin.com/{info['web_rid']}"
    return info


# --------------------------------------------------------------------------
# 主播条目
# --------------------------------------------------------------------------
def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class WatchItem:
    """watchlist.json 中的一条主播/直播间记录。"""

    name: str = ""                    # 主播名（可空，联网后由 dy_api 补全）
    target: str = ""                  # 用户原始输入（链接/房间号/分享文案）
    web_rid: str = ""                 # 直播间数字房间号
    url: str = ""                     # 直播间地址
    sec_uid: str = ""                 # 主播主页 sec_uid（用于「跳转到抖音主页」）
    auto_record: bool = True          # 开播自动录制
    quality: str = "origin"           # origin/hd/sd
    output_template: str = ""         # 目录模板，空 = 用全局配置
    enabled: bool = True              # 是否参与监控
    note: str = ""
    group: str = ""                   # 分组（GUI 批量管理用）
    last_live_at: int = 0             # 最近一次检测到开播的时间戳（0=还没记录）
    id: str = field(default_factory=_new_id)
    added_at: int = field(default_factory=lambda: int(time.time()))

    # ---------------- 序列化 ----------------
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WatchItem":
        d = dict(data or {})
        item = cls()
        for f in ("name", "target", "web_rid", "url", "sec_uid", "quality",
                  "output_template", "note", "group", "id"):
            if d.get(f) is not None:
                setattr(item, f, str(d[f]))
        item.auto_record = bool(d.get("auto_record", True))
        item.enabled = bool(d.get("enabled", True))
        try:
            item.added_at = int(d.get("added_at") or time.time())
        except (TypeError, ValueError):
            item.added_at = int(time.time())
        try:
            item.last_live_at = int(d.get("last_live_at") or 0)
        except (TypeError, ValueError):
            item.last_live_at = 0
        item.quality = normalize_quality(item.quality, "origin")
        if not item.id:
            item.id = _new_id()
        if not item.web_rid:
            parsed = parse_target_text(item.target or item.url)
            item.web_rid = parsed.get("web_rid") or ""
            if not item.url:
                item.url = parsed.get("url") or ""
        return item

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # ---------------- 便捷属性 ----------------
    @property
    def display_name(self) -> str:
        return (self.name or "").strip() or self.web_rid or (self.target or "未命名")

    @property
    def safe_name(self) -> str:
        return sanitize_filename(self.display_name, 60)

    @property
    def room_key(self) -> str:
        """唯一房间标识：优先房间号，否则用 id。"""
        return self.web_rid or self.id

    def clone(self) -> "WatchItem":
        return WatchItem.from_dict(self.to_dict())


# --------------------------------------------------------------------------
# 运行时状态（不落盘）
# --------------------------------------------------------------------------
STATUS_IDLE = "未检查"
STATUS_OFFLINE = "未开播"
STATUS_LIVE = "直播中"
STATUS_RECORDING = "录制中"
STATUS_RECONNECTING = "重连中"
STATUS_ERROR = "错误"


@dataclass
class RoomRuntime:
    """单个房间的运行时状态，供 GUI 展示与调度决策。"""

    item_id: str = ""
    status: str = STATUS_IDLE          # 中文状态文本
    is_live: bool = False
    recording: bool = False
    reconnecting: bool = False
    title: str = ""
    anchor_name: str = ""
    error: str = ""
    consecutive_failures: int = 0      # 连续查询失败次数
    last_check_at: float = 0.0
    next_check_at: float = 0.0
    record_started_at: float = 0.0
    duration_sec: int = 0
    file_size: int = 0
    segment_count: int = 0
    output_path: str = ""
    stop_reason: str = ""
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        now = time.time()
        if self.recording and self.record_started_at:
            d["duration_sec"] = int(now - self.record_started_at)
        return d


# --------------------------------------------------------------------------
# 录制事件
# --------------------------------------------------------------------------
class EventType:
    """录制/调度事件类型（字符串常量，直接可用于日志与 GUI 分发）。"""

    START = "start"              # 录制开始
    PROGRESS = "progress"        # 录制进行中（周期上报）
    STOPPED = "stopped"          # 录制结束
    ERROR = "error"              # 发生错误
    RECONNECT = "reconnect"      # 断流重连中
    SEGMENT = "segment"          # 分段文件完成
    DISK = "disk"                # 磁盘告警
    CHECK = "check"              # 状态查询结果
    INFO = "info"                # 普通提示


@dataclass
class RecordEvent:
    kind: str
    room_id: str = ""
    name: str = ""
    message: str = ""
    path: str = ""
    size: int = 0
    duration: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def format(self) -> str:
        """用于日志/GUI 面板的一行文本。"""
        parts = [f"[{self.kind}]"]
        if self.name:
            parts.append(self.name)
        if self.message:
            parts.append(self.message)
        if self.path:
            parts.append(self.path)
        return " ".join(parts)


# --------------------------------------------------------------------------
# 路径/文件名模板渲染
# --------------------------------------------------------------------------
def _safe_format(template: str, values: Dict[str, str], fallback: str) -> str:
    """用 values 渲染模板；未知占位符或语法错误时回退 fallback。"""
    if not template:
        return fallback
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return fallback


def template_values(item: WatchItem, title: str = "", now: Optional[time.struct_time] = None) -> Dict[str, str]:
    t = now or time.localtime()
    return {
        "anchor": sanitize_filename(item.display_name, 40),
        "name": sanitize_filename(item.display_name, 40),
        "room": sanitize_filename(item.web_rid or item.id, 40),
        "web_rid": item.web_rid or "",
        "date": time.strftime("%Y-%m-%d", t),
        "date8": time.strftime("%Y%m%d", t),
        "time": time.strftime("%H%M%S", t),
        "datetime": time.strftime("%Y%m%d_%H%M%S", t),
        "year": time.strftime("%Y", t),
        "month": time.strftime("%m", t),
        "day": time.strftime("%d", t),
        "title": sanitize_filename(title, 40) if title else "直播",
    }


def render_output_dir(item: WatchItem, global_template: str, output_root: Path,
                      title: str = "", now: Optional[time.struct_time] = None) -> Path:
    """按模板渲染房间输出目录（绝对路径）。"""
    values = template_values(item, title, now)
    tmpl = (item.output_template or global_template or "{anchor}/{date}").strip()
    rel = _safe_format(tmpl, values, values["anchor"] + "/" + values["date"])
    rel = rel.replace("\\", "/").strip("/")
    parts = [sanitize_filename(p, 60) for p in rel.split("/") if p.strip()]
    path = Path(output_root)
    for p in parts:
        path = path / p
    return path


def render_filename(item: WatchItem, global_template: str, title: str = "",
                    seq: int = 0, now: Optional[time.struct_time] = None) -> str:
    """按模板渲染文件名（不含扩展名）。seq>0 时追加 _rN 后缀避免重连覆盖。"""
    values = template_values(item, title, now)
    tmpl = (global_template or "{anchor}_{date}_{time}").strip()
    base = _safe_format(tmpl, values, values["anchor"] + "_" + values["datetime"])
    base = sanitize_filename(base, 120)
    if seq > 0:
        base = f"{base}_r{seq}"
    return base
