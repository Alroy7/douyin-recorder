# -*- coding: utf-8 -*-
"""tkinter 图形界面（风格 A「青屿」：清新简洁）。

结构：
  左侧导航（监控 / 录制记录 / 运行日志 / 设置 / 关于）
  监控页：搜索 + 筛选 + 多选表格 + 批量操作 + 右侧详情抽屉 + 可展开日志
  设置页：左侧分组 + 右侧表单（含最短保留时长 / ffmpeg 自动检测 / 日志与诊断包）
后台线程跑监控循环，UI 定时轮询 snapshot 与事件队列，不阻塞。
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

from . import disk as diskmod
from . import ffmpeg_util
from .config import Config
from .manager import MonitorManager
from . import import_util
from . import __version__
from .model import EventType, RecordEvent, WatchItem, parse_target_text
from .storage import Watchlist
from . import ui_theme as T

LOG = logging.getLogger("dyrec.gui")

def center_on_parent(win: tk.Misc, parent: Optional[tk.Misc] = None,
                     vertical_bias: float = 3.0) -> None:
    """把子窗口（Toplevel/对话框）摆到父窗口中间，避免出现在屏幕左上角。"""
    try:
        win.update_idletasks()
        pw = parent if parent is not None else getattr(win, "master", None)
        if pw is None:
            return
        px, py = pw.winfo_rootx(), pw.winfo_rooty()
        pwd, ph = pw.winfo_width(), pw.winfo_height()
        w, h = win.winfo_width(), win.winfo_height()
        if pwd <= 1 or ph <= 1:
            pwd = pw.winfo_screenwidth()
            ph = pw.winfo_screenheight()
            px = py = 0
        x = px + (pwd - w) // 2
        y = py + int(max(0, (ph - h) / max(1.0, vertical_bias)))
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, min(x, max(0, sw - w)))
        y = max(0, min(y, max(0, sh - h)))
        win.geometry("+%d+%d" % (x, y))
    except Exception:
        pass


QUALITY_CHOICES = [
    ("跟随最高（原画优先）", "origin"),
    ("自动最高（按分辨率/码率）", "best"),
    ("高清 720p", "hd"),
    ("标清 540p", "sd"),
]
CONTAINER_CHOICES = [("FLV（推荐，直录最稳）", "flv"), ("TS", "ts")]
OUTPUT_TEMPLATE_CHOICES = [
    ("主播 / 日期（默认）", "{anchor}/{date}"),
    ("仅主播（全部文件放主播目录）", "{anchor}"),
    ("日期 / 主播", "{date}/{anchor}"),
    ("标题 / 日期", "{title}/{date}"),
    ("自定义…", "__custom__"),
]
PAGE_TITLES = {
    "monitor": ("直播监控", "长期挂机 · 多主播监控与录制"),
    "history": ("录制记录", "每场录制的开始/结束/大小/状态"),
    "logs": ("运行日志", "按天保存，可导出诊断包发我分析"),
    "settings": ("设置", "修改后点「保存设置」生效"),
    "about": ("关于", "本地自用版 · 不登录 · 不上传"),
}


def quality_label(value: str) -> str:
    for label, val in QUALITY_CHOICES:
        if val == value:
            return label
    return QUALITY_CHOICES[0][0]


def quality_value(label: str) -> str:
    for lab, val in QUALITY_CHOICES:
        if lab == label:
            return val
    return "origin"


def fmt_duration(sec: Any) -> str:
    try:
        s = int(float(sec or 0))
    except Exception:
        return "—"
    if s <= 0:
        return "00:00"
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def fmt_size(num: Any) -> str:
    try:
        n = float(num or 0)
    except Exception:
        return "—"
    if n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % int(n))
        n /= 1024.0
    return "—"


class GuiLogHandler(logging.Handler):
    """把日志记录投递到 GUI 队列（线程安全）。"""

    def __init__(self, log_queue: queue.Queue):
        logging.Handler.__init__(self)
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put_nowait(record)
        except Exception:  # pragma: no cover
            pass


class _DialogBase(tk.Toplevel):
    """模态对话框基类（风格化）。"""

    def __init__(self, parent: tk.Misc, title: str, width: int = 560, height: int = 420):
        super().__init__(parent)
        self.title(title)
        self.result: Optional[Any] = None
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.configure(bg=T.BG)
        self._body = tk.Frame(self, bg=T.CARD, highlightbackground=T.LINE,
                              highlightthickness=1)
        self._body.pack(fill="both", expand=True, padx=16, pady=16)
        self.geometry("%dx%d" % (width, height))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _e: self.destroy())

    def add_buttons(self, ok_text: str = "确定", cancel_text: str = "取消",
                    on_ok_text: Optional[str] = None) -> tk.Frame:
        bar = tk.Frame(self._body, bg=T.CARD)
        bar.pack(side="bottom", fill="x", pady=(10, 0))
        T.RButton(bar, ok_text, command=self.on_ok, kind="primary", width=108,
                  height=34, bg_parent=T.CARD).pack(side="right", padx=(8, 0))
        T.RButton(bar, cancel_text, command=self.destroy, kind="ghost", width=80,
                  height=34, bg_parent=T.CARD).pack(side="right")
        return bar

    def finish(self) -> None:
        self.update_idletasks()
        center_on_parent(self, self.master)
        self.after(50, self.focus_force)

    def on_ok(self) -> None:  # 子类实现
        self.destroy()


class ItemDialog(_DialogBase):
    """添加/编辑主播。支持粘贴房间号、直播链接、分享短链、主播主页链接。"""

    def __init__(self, parent: tk.Misc, item: Optional[WatchItem] = None,
                 title: str = "添加主播"):
        super().__init__(parent, title, width=640, height=500)
        self._item = item
        body = self._body
        T.section_title(body, "直播间链接 / 房间号 / 分享文案").pack(anchor="w", padx=18, pady=(12, 4))
        self.text = tk.Text(body, height=4, bd=0, bg="#F8FAFC", fg=T.TEXT, font=T.font(10),
                            highlightthickness=1, highlightbackground=T.INPUT_LINE,
                            highlightcolor=T.PRIMARY, padx=8, pady=6, wrap="word")
        self.text.pack(fill="x", padx=18)
        if item is not None:
            self.text.insert("1.0", item.target or item.url or item.web_rid or "")
        T.hint_label(body, "支持：房间号 / live.douyin.com 链接 / v.douyin.com 短链 / 主播主页链接（可粘贴整段分享文案）").pack(
            anchor="w", padx=18, pady=(4, 8))

        row = tk.Frame(body, bg=T.CARD)
        row.pack(fill="x", padx=18, pady=4)
        tk.Label(row, text="名称", bg=T.CARD, fg=T.TEXT, font=T.font(10), width=8, anchor="w").pack(side="left")
        self.name_var = tk.StringVar(value=(item.name if item else ""))
        T.make_entry(row, self.name_var, 36).pack(side="left", fill="x", expand=True, ipady=4)

        row2 = tk.Frame(body, bg=T.CARD)
        row2.pack(fill="x", padx=18, pady=4)
        tk.Label(row2, text="画质", bg=T.CARD, fg=T.TEXT, font=T.font(10), width=8, anchor="w").pack(side="left")
        self.quality_var = tk.StringVar(value=quality_label(item.quality if item else "origin"))
        ttk.Combobox(row2, textvariable=self.quality_var, values=[x[0] for x in QUALITY_CHOICES],
                     state="readonly", width=18).pack(side="left")
        self.auto_cb = T.CheckBox(row2, "开播自动录制",
                                  value=(item.auto_record if item else True),
                                  bg_parent=T.CARD)
        self.auto_cb.pack(side="left", padx=(20, 0))
        self.enabled_cb = T.CheckBox(row2, "参与监控",
                                     value=(item.enabled if item else True),
                                     bg_parent=T.CARD)
        self.enabled_cb.pack(side="left", padx=(14, 0))

        row3 = tk.Frame(body, bg=T.CARD)
        row3.pack(fill="x", padx=18, pady=4)
        tk.Label(row3, text="输出模板", bg=T.CARD, fg=T.TEXT, font=T.font(10), width=8, anchor="w").pack(side="left")
        self.template_var = tk.StringVar(value=(item.output_template if item else ""))
        T.make_entry(row3, self.template_var, 30).pack(side="left", fill="x", expand=True, ipady=4)
        T.hint_label(body, "可用占位符：{anchor} {room} {date} {time} {title}（留空用默认 {anchor}/{date}）").pack(
            anchor="w", padx=18, pady=(2, 8))

        self.add_buttons("保存" if item else "添加")
        self.finish()

    def on_ok(self) -> None:
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("提示", "请填写房间号或直播间链接", parent=self)
            return
        self.result = {
            "text": text,
            "name": self.name_var.get().strip(),
            "auto_record": bool(self.auto_cb.get()),
            "enabled": bool(self.enabled_cb.get()),
            "quality": quality_value(self.quality_var.get()),
            "output_template": self.template_var.get().strip(),
        }
        self.destroy()


class BatchImportDialog(_DialogBase):
    """批量导入（一行一个链接/房间号），实时统计识别数量。"""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent, "批量导入直播间", width=780, height=620)
        body = self._body
        T.section_title(body, "批量导入直播间").pack(anchor="w", padx=18, pady=(12, 2))
        T.hint_label(body, "一行一个：直播间链接 / 手机分享短链 / 房间号 / 主播主页链接；无法识别的行会自动跳过").pack(
            anchor="w", padx=18, pady=(0, 8))
        self.text = tk.Text(body, height=10, bd=0, bg="#F8FAFC", fg=T.TEXT, font=T.font(10),
                            highlightthickness=1, highlightbackground=T.INPUT_LINE,
                            highlightcolor=T.PRIMARY, padx=8, pady=6, wrap="none")
        self.text.pack(fill="both", expand=True, padx=18)
        self.text.bind("<KeyRelease>", lambda _e: self._update_count())
        self._file_records: List[Dict[str, str]] = []
        self._file_dups = 0
        self._file_invalid = 0
        self._file_skipped = 0
        file_row = tk.Frame(body, bg=T.CARD)
        file_row.pack(fill="x", padx=18, pady=(6, 0))
        T.RButton(file_row, "从 Excel / CSV 文件导入…", command=self._pick_file,
                  kind="ghost", width=180, height=30, bg_parent=T.CARD).pack(side="left")
        self._file_label = T.hint_label(file_row, "支持 .xlsx / .csv；与粘贴内容合并去重", color=T.MUTED)
        self._file_label.pack(side="left", padx=(10, 0))
        self.count_label = T.hint_label(body, "已识别 0 个有效直播间", color=T.OK)
        self.count_label.pack(anchor="w", padx=18, pady=(6, 2))

        row = tk.Frame(body, bg=T.CARD)
        row.pack(fill="x", padx=18, pady=6)
        tk.Label(row, text="默认画质", bg=T.CARD, fg=T.SUB, font=T.font(10)).pack(side="left")
        self.quality_var = tk.StringVar(value=QUALITY_CHOICES[0][0])
        ttk.Combobox(row, textvariable=self.quality_var, values=[x[0] for x in QUALITY_CHOICES],
                     state="readonly", width=18).pack(side="left", padx=(6, 18))
        tk.Label(row, text="自动录制", bg=T.CARD, fg=T.SUB, font=T.font(10)).pack(side="left")
        self.auto_cb = T.CheckBox(row, "开播自动录制", value=True, bg_parent=T.CARD)
        self.auto_cb.pack(side="left", padx=(6, 18))
        tk.Label(row, text="分组", bg=T.CARD, fg=T.SUB, font=T.font(10)).pack(side="left")
        self.group_var = tk.StringVar(value="默认")
        T.make_entry(row, self.group_var, 10).pack(side="left", padx=(6, 0), ipady=4)

        T.hint_label(body, "提示：导入 1000+ 直播间后，程序会按设置间隔分批轮询；总带宽默认不限，可在设置页调整。").pack(
            anchor="w", padx=18, pady=(2, 6))
        bar = tk.Frame(body, bg=T.CARD)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        T.RButton(bar, "开始导入", command=self.on_ok, kind="primary", width=120, height=34,
                  bg_parent=T.CARD).pack(side="right", padx=(8, 0))
        T.RButton(bar, "取消", command=self.destroy, kind="ghost", width=80, height=34,
                  bg_parent=T.CARD).pack(side="right")
        self.finish()
        self._update_count()

    def _lines(self) -> List[str]:
        return [ln.strip() for ln in self.text.get("1.0", "end").splitlines() if ln.strip()]

    def _pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择主播列表文件",
            filetypes=[("Excel / CSV", "*.xlsx *.xlsm *.csv *.tsv *.txt"),
                       ("Excel", "*.xlsx *.xlsm"), ("CSV", "*.csv *.tsv *.txt"),
                       ("所有文件", "*.*")],
            parent=self)
        if not path:
            return
        try:
            records, skipped, sheet_info = import_util.records_from_file(path)
            accepted, dup, invalid = import_util.dedupe(records)
        except Exception as exc:
            messagebox.showwarning("读取失败", "无法读取该文件：%s" % exc, parent=self)
            return
        self._file_records = accepted
        self._file_dups = dup
        self._file_invalid = skipped + invalid
        self._file_skipped = skipped + invalid
        name = os.path.basename(path)
        total_rows = sum(int(info.get("rows") or 0) for info in sheet_info)
        total_sheets = len(sheet_info)
        empty_n = len([i for i in sheet_info if int(i.get("rows") or 0) <= 0])
        sheet_desc = "%d 个 sheet" % total_sheets
        if empty_n:
            sheet_desc += "（%d 个空表）" % empty_n
        self._file_label.configure(
            text="%s：%s · %d 行 · 有效 %d · 重复 %d · 无效 %d" % (
                name, sheet_desc, total_rows, len(accepted), dup, skipped + invalid),
            color=T.OK if accepted else T.WARN)
        self._update_count()

    def _combined_records(self):
        records = list(self._file_records) + import_util.records_from_lines(self._lines())
        accepted, dup, invalid = import_util.dedupe(records)
        return accepted, dup, invalid

    def _valid_count(self) -> int:
        accepted, _dup, _invalid = self._combined_records()
        return len(accepted)

    def _update_count(self) -> None:
        accepted, dup, invalid = self._combined_records()
        self.count_label.configure(
            text="将导入 %d 个 · 已存在（重复）%d 个 · 无法识别 %d 个"
                 % (len(accepted), dup, invalid))

    def on_ok(self) -> None:
        records, dup, invalid = self._combined_records()
        lines = self._lines()
        if not records:
            messagebox.showwarning("提示", "请粘贴或选择要导入的主播列表", parent=self)
            return
        self.result = {
            "records": records,
            "lines": lines,
            "quality": quality_value(self.quality_var.get()),
            "auto_record": bool(self.auto_cb.get()),
            "group": self.group_var.get().strip(),
            "dup": dup,
            "invalid": invalid,
            "file_skipped": self._file_skipped,
        }
        self.destroy()


class BatchSetDialog(_DialogBase):
    """批量设置选中主播的属性。"""

    FIELDS = [
        ("auto_record", "开播自动录制", ["开启", "关闭"]),
        ("quality", "画质", [x[0] for x in QUALITY_CHOICES]),
        ("enabled", "参与监控", ["开启", "关闭"]),
        ("group", "分组", None),
    ]

    def __init__(self, parent: tk.Misc, count: int = 0):
        super().__init__(parent, "批量设置", width=460, height=340)
        body = self._body
        T.section_title(body, "批量设置（已选 %d 个主播）" % count).pack(anchor="w", padx=18, pady=(14, 10))
        row = tk.Frame(body, bg=T.CARD)
        row.pack(fill="x", padx=18, pady=6)
        tk.Label(row, text="设置项", bg=T.CARD, fg=T.TEXT, font=T.font(10), width=8,
                 anchor="w").pack(side="left")
        self.field_var = tk.StringVar(value="开播自动录制")
        self.field_combo = ttk.Combobox(row, textvariable=self.field_var, state="readonly",
                                        values=[f[1] for f in self.FIELDS], width=18)
        self.field_combo.pack(side="left")
        self.field_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_value())
        row2 = tk.Frame(body, bg=T.CARD)
        row2.pack(fill="x", padx=18, pady=6)
        tk.Label(row2, text="值", bg=T.CARD, fg=T.TEXT, font=T.font(10), width=8,
                 anchor="w").pack(side="left")
        self.value_var = tk.StringVar(value="开启")
        self.value_combo = ttk.Combobox(row2, textvariable=self.value_var, state="readonly",
                                        values=["开启", "关闭"], width=18)
        self.value_combo.pack(side="left")
        self.group_var = tk.StringVar(value="默认")
        self.group_entry = T.make_entry(row2, self.group_var, 18)
        self.group_entry.pack(side="left", padx=(6, 0), ipady=4)
        self.group_entry.pack_forget()
        T.hint_label(body, "提示：批量设置会立即保存到主播列表，正在录制的场次不受影响").pack(
            anchor="w", padx=18, pady=(2, 6))
        self.add_buttons("应用到选中")
        self.finish()

    def _update_value(self) -> None:
        field = self.field_var.get()
        if field == "分组":
            self.value_combo.pack_forget()
            self.group_entry.pack(side="left", padx=(6, 0), ipady=4)
        else:
            self.group_entry.pack_forget()
            self.value_combo.pack(side="left")
            for key, label, choices in self.FIELDS:
                if label == field and choices:
                    self.value_combo.configure(values=choices)
                    self.value_var.set(choices[0])
                    break

    def on_ok(self) -> None:
        field = self.field_var.get()
        if field == "分组":
            self.result = {"field": "group", "value": self.group_var.get().strip()}
        else:
            key = None
            for k, label, _choices in self.FIELDS:
                if label == field:
                    key = k
                    break
            value = self.value_var.get()
            if field == "画质":
                value = quality_value(value)
            else:
                value = (value == "开启")
            self.result = {"field": key, "value": value}
        self.destroy()


class ActivityDialog(tk.Toplevel):
    """主播活跃度窗口：最近开播时间、多久没开播。"""

    def __init__(self, parent: tk.Misc, rows):
        super().__init__(parent)
        self.title("主播活跃度")
        self.geometry("860x580")
        self.configure(bg=T.BG)
        self.transient(parent)
        center_on_parent(self, parent)
        self.rows = list(rows or [])
        self.grab_set()
        head = tk.Frame(self, bg=T.BG)
        head.pack(fill="x", padx=20, pady=(14, 6))
        tk.Label(head, text="主播活跃度（按最近开播时间排序）", bg=T.BG, fg=T.TEXT,
                 font=T.font(14, True), anchor="w").pack(side="left")
        never = sum(1 for r in self.rows if not r.get("last_live_at"))
        over7 = sum(1 for r in self.rows
                    if r.get("last_live_at") and (r.get("days") or 0) > 7)
        over30 = sum(1 for r in self.rows
                     if r.get("last_live_at") and (r.get("days") or 0) > 30)
        tk.Label(head,
                 text="共 %d 个 · 从未见开播 %d · 超 7 天未开播 %d · 超 30 天 %d"
                      % (len(self.rows), never, over7, over30),
                 bg=T.BG, fg=T.SUB, font=T.font(9), anchor="w").pack(side="left", padx=(14, 0))

        card = T.Card(self)
        card.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        inner = card.inner
        cols = ("name", "room", "group", "last", "ago", "days")
        headings = {"name": "主播", "room": "房间号", "group": "分组",
                    "last": "最近开播时间", "ago": "距今", "days": "未开播天数"}
        tree = ttk.Treeview(inner, columns=cols, show="headings")
        widths = {"name": 200, "room": 120, "group": 90, "last": 150, "ago": 100,
                  "days": 90}
        for c in cols:
            tree.heading(c, text=headings[c])
            tree.column(c, width=widths[c], anchor="w",
                        stretch=(c in ("name",)))
        vsb = ttk.Scrollbar(inner, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6, padx=(0, 6))
        for r in self.rows:
            ts = int(r.get("last_live_at") or 0)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "从未"
            days = "从未" if not ts else "%.1f 天" % (r.get("days") or 0)
            tree.insert("", "end", values=(r.get("name") or "", r.get("room") or "-",
                                           r.get("group") or "默认", when,
                                           r.get("ago") or "从未", days))
        bar = tk.Frame(self, bg=T.BG)
        bar.pack(fill="x", padx=20, pady=(0, 14))
        T.RButton(bar, "导出 CSV", command=self._export_csv, kind="primary",
                  width=110, height=32, bg_parent=T.BG).pack(side="right")
        T.RButton(bar, "关闭", command=self.destroy, kind="ghost", width=80,
                  height=32, bg_parent=T.BG).pack(side="right", padx=(0, 8))

    def _export_csv(self) -> None:
        from . import activity
        default = "主播活跃度_%s.csv" % time.strftime("%Y%m%d_%H%M")
        path = filedialog.asksaveasfilename(
            title="导出主播活跃度", defaultextension=".csv", initialfile=default,
            filetypes=[("CSV 文件", "*.csv")], parent=self)
        if not path:
            return
        try:
            n = activity.write_csv(self.rows, path)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        messagebox.showinfo("导出成功", "已导出 %d 个主播到：\n%s" % (n, path), parent=self)


class SettingsDialog(_DialogBase):
    """简单设置对话框（兼容旧接口；主设置页在「设置」Tab）。"""

    def __init__(self, parent: tk.Misc, config: Config):
        super().__init__(parent, "设置", width=520, height=200)
        self.cfg = config
        T.section_title(self._body, "完整设置请使用主界面左侧「设置」页").pack(anchor="w", padx=18, pady=(20, 6))
        T.hint_label(self._body, "此对话框仅保留兼容入口。您可以直接在主窗口切换设置分组并保存。").pack(
            anchor="w", padx=18)
        self.add_buttons("知道了", cancel_text="关闭")
        self.finish()

    def on_ok(self) -> None:
        self.destroy()


# 设置页以「分钟」展示、内部仍按「秒」存储的字段
_MINUTE_SETTING_KEYS = ("min_keep_seconds", "segment_duration")


def _seconds_to_min_text(seconds) -> str:
    try:
        minutes = float(seconds) / 60.0
    except (TypeError, ValueError):
        return "0"
    if abs(minutes - round(minutes)) < 1e-9:
        return str(int(round(minutes)))
    return ("%.2f" % minutes).rstrip("0").rstrip(".")


SETTINGS_GROUPS = [
    ("常规", [
        ("output_dir", "输出根目录", "dir", "录像保存位置"),
        ("ffmpeg_path", "ffmpeg 路径", "ffmpeg", "留空自动检测"),
    ]),
    ("录制", [
        ("container", "录制容器", "combo", "FLV 直录最稳"),
        ("min_keep_seconds", "最短保留时长（分钟）", "int", "录制总时长低于此值自动删除，0=关闭；支持小数如 0.5"),
        ("segment_duration", "分段时长（分钟，0=不分段）", "int", "支持小数，如 30 分钟填 30"),
        ("segment_size_mb", "单个分段大小（MB，0=不限）", "int", ""),
        ("segment_remux_now", "分段结束立即转 MP4", "bool", "开启后每段录完立刻转 mp4 并删源，避免整场结束才转占空间（需设置分段时长>0）"),
        ("remux_mp4", "录制结束转 MP4（不转码）", "bool", "停止监控/关闭软件时会先把正在录制的文件转成 MP4 再退出"),
        ("keep_source", "转 MP4 后保留原文件", "bool", ""),
        ("remux_concurrency", "同时转 MP4 数量", "int", "1~8，默认 2；避免多路同时 remux 写爆磁盘"),
        ("shutdown_wait_seconds", "退出时等待转换上限（秒）", "int", "停止监控或关闭软件时等待收尾/转 MP4 的最长时间，0=一直等待"),
        ("output_template", "保存目录结构", "combo", "默认「主播/日期」；选「仅主播」则所有录像直接放在主播目录下"),
        ("output_template_custom", "自定义目录模板", "str", "仅当结构选「自定义」时生效；可用 {anchor} {date} {time} {title} {room}"),
    ]),
    ("清晰度", [
        ("quality", "默认清晰度", "combo", "原画优先=默认最高档；自动最高按分辨率/码率选"),
        ("quality_degrade_enabled", "断流自动降档", "bool", ""),
        ("quality_degrade_after_reconnects", "连续断流几次后降档", "int", "默认 2 次，按阶梯下降"),
        ("quality_ladder", "降档阶梯", "str", "逗号分隔，默认 origin,hd,sd"),
    ]),
    ("性能", [
        ("max_concurrent", "同时录制上限（0=不限）", "int", "0=不限，按带宽/进程/磁盘自动调度"),
        ("max_total_mbps", "总带宽预算（Mbps，0=不限）", "int", "超出预算的房间排队（原画约 6.4Mbps/路）"),
        ("max_ffmpeg_processes", "ffmpeg 进程上限（0=不限）", "int", "默认 300，防止进程过多拖垮系统"),
        ("retry_max", "断流重连次数", "int", ""),
        ("retry_interval", "重连间隔（秒）", "int", ""),
        ("stall_check_seconds", "卡死检查间隔（秒，0=关闭）", "int", ""),
        ("keep_days", "录像保留天数（0=不限）", "int", ""),
        ("keep_max_files", "每目录最多保留文件（0=不限）", "int", ""),
        ("min_free_gb", "最小剩余空间（GB）", "float", ""),
    ]),
    ("轮询", [
        ("max_poll_workers", "轮询线程数", "int", "默认 12，1000+ 房间可加到 16-24"),
        ("poll_rate_limit", "轮询速率（次/秒）", "int", "默认 6；1000+ 房间建议 6-10，太高易触发风控"),
        ("poll_interval_live", "直播中复查间隔（秒）", "int", "默认 30，用于快速发现下播/断流"),
        ("poll_interval_warm", "近期开播间隔（秒）", "int", "默认 90（24 小时内开播过）"),
        ("poll_interval_cold", "长期离线间隔（秒）", "int", "默认 300，降低风控与流量"),
        ("resolve_workers", "链接解析线程数", "int", "批量导入时解析主页链接/短链，默认 3"),
        ("invalid_auto_disable_after", "无效主播自动停用阈值", "int", "连续 N 次确认条目无效（只有昵称/房间号错）后自动停用，0=不停用；右键可重新启用"),
    ]),
    ("通知", [
        ("notify.enabled", "启用通知（群机器人/声音）", "bool", ""),
        ("notify.sound", "开播提示音", "bool", ""),
        ("notify.webhooks", "群机器人 Webhook（每行一个）", "text", "企微/钉钉/飞书"),
    ]),
    ("运行记录", [
        ("log_keep_days", "日志保留天数", "int", "超期自动清理"),
        ("log_level", "日志级别", "combo", ""),
    ]),
]


class FfmpegMissingDialog(_DialogBase):
    """缺少 ffmpeg 时的提示对话框：下载 / 手动选择 / 稍后。"""

    def __init__(self, parent: tk.Misc, config: Config, hint: str = ""):
        super().__init__(parent, "未找到 ffmpeg", width=660, height=330)
        self.cfg = config
        body = self._body
        T.section_title(body, "未找到可用的 ffmpeg").pack(anchor="w", padx=18, pady=(16, 6))
        T.hint_label(body, hint or "录制需要 ffmpeg。请下载后放到项目 bin 目录，或在设置里指定路径。",
                     bg_parent=T.CARD).pack(anchor="w", padx=18)
        T.hint_label(body, "推荐：gyan.dev 的 Windows 版本（解压后把 ffmpeg.exe 放到项目 bin/ 目录）",
                     bg_parent=T.CARD, color=T.SUB).pack(anchor="w", padx=18, pady=(6, 0))
        bar = tk.Frame(body, bg=T.CARD)
        bar.pack(side="bottom", fill="x", pady=(10, 0))
        T.RButton(bar, "打开下载页", command=lambda: ffmpeg_util.open_download_page(),
                  kind="primary", width=110, height=34, bg_parent=T.CARD).pack(
            side="right", padx=(8, 0))
        T.RButton(bar, "手动选择", command=self._pick, kind="ghost", width=96, height=34,
                  bg_parent=T.CARD).pack(side="right", padx=(8, 0))
        T.RButton(bar, "稍后", command=self.destroy, kind="text", width=64, height=34,
                  bg_parent=T.CARD).pack(side="right")
        self.finish()

    def _pick(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 ffmpeg.exe", filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
            parent=self)
        if not path:
            return
        self.cfg["ffmpeg_path"] = path
        self.cfg.save()
        self.result = path
        self.destroy()


class RecorderApp:
    """主窗口。"""

    def __init__(self, config: Config, watchlist: Watchlist):
        self.cfg = config
        self.watchlist = watchlist
        self._closing = False
        self._event_q: "queue.Queue[Any]" = queue.Queue()
        self._log_q: "queue.Queue[Any]" = queue.Queue()
        self._state_rows: Dict[str, Dict[str, Any]] = {}
        self._value_cache: Dict[str, tuple] = {}
        self._visible: List[Dict[str, Any]] = []
        self._filter = "all"
        self._search = ""
        self._page = 0
        self._page_size = 200
        self._checked: set = set()   # 表格勾选框选中的主播 id
        self._sort_col: Optional[str] = None   # 默认保持列表顺序（稳定，不随状态跳动）
        self._sort_desc = False
        self._detail_visible = True
        self._cover_photo = None
        self._cover_cache: Dict[str, tuple] = {}   # item_id -> (png, ts, photo)
        self._cover_pending: set = set()
        self._cover_results: "queue.Queue[Any]" = queue.Queue()
        self._preview_win: Optional[tk.Toplevel] = None
        self._preview_after: Optional[str] = None
        self._closing_dialog: Optional[tk.Toplevel] = None
        self._closing_label: Optional[tk.Label] = None
        self._close_done_event = threading.Event()
        self._close_result: bool = True
        self._log_visible = False
        self._monitor_started_at = 0.0
        self._settings_group = "常规"
        self._settings_draft: Dict[str, Any] = {}
        self._setting_vars: Dict[str, Any] = {}
        self._history_rows: List[Dict[str, Any]] = []
        self._short_deleted = 0

        self.manager = MonitorManager(config, watchlist,
                                      on_event=self._on_manager_event, logger=LOG)

        self.root = tk.Tk()
        self.root.title("抖音直播录制工具 · 本地版")
        T.init_dpi(self.root)
        T.init_fonts(self.root)
        T.setup_styles(self.root)
        self.root.geometry("1440x900")
        self.root.minsize(1180, 740)

        self._log_handler = GuiLogHandler(self._log_q)
        self._log_handler.setLevel(logging.INFO)
        LOG.addHandler(self._log_handler)

        self._pages: Dict[str, tk.Frame] = {}
        self._nav_items: Dict[str, T.NavItem] = {}
        self._build_layout()
        self._select_page("monitor")
        self._refresh_table(force=True)
        self._reload_setting_draft()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self._poll_events)
        self.root.after(200, self._poll_logs)
        self.root.after(max(300, self.cfg.get_int("ui_refresh_ms", 1000) or 1000),
                        self._refresh_loop)
        self.root.after(400, self._check_ffmpeg_on_start)

    # ==================================================================
    # 布局
    # ==================================================================
    def _build_layout(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)
        self._build_sidebar()
        main = tk.Frame(self.root, bg=T.BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)
        self._content = tk.Frame(main, bg=T.BG)
        self._content.grid(row=0, column=0, sticky="nsew")
        self._statusbar = tk.Frame(main, bg=T.CARD, height=30)
        self._statusbar.grid(row=1, column=0, sticky="ew")
        self._statusbar.grid_propagate(False)
        tk.Frame(self._statusbar, bg=T.LINE, height=1).pack(fill="x", side="top")
        self._status_left = tk.Label(self._statusbar, text="", bg=T.CARD, fg=T.SUB,
                                     font=T.font(9))
        self._status_left.pack(side="left", padx=16, pady=5)
        self._status_right = tk.Label(self._statusbar, text="", bg=T.CARD, fg=T.MUTED,
                                      font=T.font(9))
        self._status_right.pack(side="right", padx=16, pady=5)

        self._build_page_monitor()
        self.root.after(120, self._init_paned_position)
        self._build_page_history()
        self._build_page_logs()
        self._build_page_settings()
        self._build_page_about()

    def _init_paned_position(self) -> None:
        """首次布局后把分栏拖动条放到靠右位置，让列表默认更大。"""
        try:
            total = self._paned.winfo_width()
            if total > 500:
                self._paned.sashpos(0, max(360, total - 300))
        except Exception:
            pass

    def _build_sidebar(self) -> None:
        side = tk.Frame(self.root, bg=T.SIDEBAR_BG, width=200)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        tk.Frame(self.root, bg=T.LINE, width=1).place(x=200, y=0, relheight=1.0)
        logo = tk.Frame(side, bg=T.SIDEBAR_BG)
        logo.pack(fill="x", padx=20, pady=(18, 14))
        cv = tk.Canvas(logo, width=36, height=36, bg=T.SIDEBAR_BG, highlightthickness=0)
        cv.pack(side="left")
        T._round_rect(cv, 0, 0, 36, 36, 10, fill=T.PRIMARY, outline="")
        cv.create_text(18, 18, text="抖", font=T.font(15, True), fill="#FFFFFF")
        txt = tk.Frame(logo, bg=T.SIDEBAR_BG)
        txt.pack(side="left", padx=(10, 0))
        tk.Label(txt, text="抖音录制", bg=T.SIDEBAR_BG, fg=T.TEXT, font=T.font(13, True),
                 anchor="w").pack(anchor="w")
        tk.Label(txt, text="本地版 v%s" % __version__, bg=T.SIDEBAR_BG, fg=T.MUTED, font=T.font(9),
                 anchor="w").pack(anchor="w")

        nav_defs = [("monitor", "▤", "监控"), ("history", "☰", "录制记录"),
                    ("logs", "≡", "运行日志"), ("settings", "⚙", "设置"),
                    ("about", "ⓘ", "关于")]
        holder = tk.Frame(side, bg=T.SIDEBAR_BG)
        holder.pack(fill="x", pady=(4, 0))
        for key, icon, label in nav_defs:
            item = T.NavItem(holder, icon, label, lambda _k=key: self._select_page(_k))
            item.pack(fill="x", padx=8, pady=2)
            self._nav_items[key] = item

        bottom = tk.Frame(side, bg=T.SIDEBAR_BG)
        bottom.pack(side="bottom", fill="x", padx=20, pady=16)
        tk.Frame(bottom, bg=T.LINE, height=1).pack(fill="x", pady=(0, 10))
        self._side_status = tk.Label(bottom, text="● 运行正常", bg=T.SIDEBAR_BG, fg=T.OK,
                                     font=T.font(9), anchor="w")
        self._side_status.pack(anchor="w")
        tk.Label(bottom, text="无需登录 · 仅本机运行", bg=T.SIDEBAR_BG, fg=T.MUTED,
                 font=T.font(8), anchor="w").pack(anchor="w", pady=(2, 0))

    def _new_page(self, key: str) -> tk.Frame:
        page = tk.Frame(self._content, bg=T.BG)
        self._pages[key] = page
        return page

    def _page_header(self, parent: tk.Frame, key: str,
                     actions: Optional[List[tk.Widget]] = None) -> tk.Frame:
        title, subtitle = PAGE_TITLES.get(key, (key, ""))
        head = tk.Frame(parent, bg=T.BG)
        head.pack(fill="x", padx=24, pady=(16, 10))
        box = tk.Frame(head, bg=T.BG)
        box.pack(side="left")
        tk.Label(box, text=title, bg=T.BG, fg=T.TEXT, font=T.font(20, True),
                 anchor="w").pack(anchor="w")
        self._page_subtitle = tk.Label(box, text=subtitle, bg=T.BG, fg=T.SUB,
                                       font=T.font(10), anchor="w")
        self._page_subtitle.pack(anchor="w", pady=(2, 0))
        act = tk.Frame(head, bg=T.BG)
        act.pack(side="right")
        for w in (actions or []):
            w.pack(side="left", padx=(8, 0))
        return head

    # ---------------- 监控页 ----------------
    def _build_page_monitor(self) -> None:
        page = self._new_page("monitor")
        self._page_monitor = page

        head = tk.Frame(page, bg=T.BG)
        head.pack(fill="x", padx=24, pady=(16, 8))
        box = tk.Frame(head, bg=T.BG)
        box.pack(side="left")
        self._monitor_title = tk.Label(box, text="直播监控", bg=T.BG, fg=T.TEXT,
                                       font=T.font(20, True), anchor="w")
        self._monitor_title.pack(anchor="w")
        self._monitor_sub = tk.Label(box, text="", bg=T.BG, fg=T.SUB, font=T.font(10),
                                     anchor="w")
        self._monitor_sub.pack(anchor="w", pady=(2, 0))
        act = tk.Frame(head, bg=T.BG)
        act.pack(side="right")
        # 注意：按钮必须以目标容器为父控件创建；若父控件是 page 而用 pack(in_=act)，
        # 后创建的 act/bar 帧会盖住按钮（Windows 上表现为按钮“消失”）。
        self._btn_start = T.RButton(act, "开始监控", command=self.on_start, kind="primary",
                                    width=104, height=34)
        self._btn_stop = T.RButton(act, "停止监控", command=self.on_stop, kind="danger",
                                   width=104, height=34)
        for w in (self._btn_stop, self._btn_start):
            w.pack(side="left", padx=(8, 0))

        # 工具行：导入/添加/导出（左）+ 搜索 + 批量操作（右）
        bar = tk.Frame(page, bg=T.BG)
        bar.pack(fill="x", padx=24, pady=(2, 8))
        self._btn_batch_import = T.RButton(bar, "＋ 导入直播间", command=self._batch_import,
                                           kind="ghost", width=116, height=34)
        self._btn_add = T.RButton(bar, "＋ 添加", command=self.on_add, kind="primary",
                                  width=80, height=34)
        self._btn_export = T.RButton(bar, "导出列表", command=self.export_watchlist,
                                     kind="ghost", width=88, height=34)
        for w in (self._btn_batch_import, self._btn_add, self._btn_export):
            w.pack(side="left", padx=(0, 8))
        search = tk.Frame(bar, bg=T.CARD, highlightbackground=T.INPUT_LINE,
                          highlightthickness=1)
        search.pack(side="left", ipady=3)
        tk.Label(search, text="⌕", bg=T.CARD, fg=T.MUTED, font=T.font(11)).pack(side="left",
                                                                                padx=(8, 2))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_a: self._on_search())
        e = T.make_entry(search, self._search_var, 22)
        e.pack(side="left", ipady=3, padx=(0, 8))
        self._search_placeholder = tk.Label(search, text="搜索主播 / 房间号 / 分组",
                                            bg=T.CARD, fg=T.MUTED, font=T.font(9))
        self._search_placeholder.place(x=30, y=5)
        e.bind("<FocusIn>", lambda _e: self._search_placeholder.place_forget())
        e.bind("<FocusOut>", lambda _e: (None if self._search_var.get()
                                         else self._search_placeholder.place(x=30, y=5)))
        # 占位文字盖在输入框上方，点击它要能聚焦到输入框（否则看起来“点了没反应”）
        self._search_placeholder.bind("<Button-1>", lambda _e: e.focus_set())
        search.bind("<Button-1>", lambda _e: e.focus_set())
        self._search_entry = e

        self._btn_batch_rec = T.RButton(bar, "批量录制 ▾", command=self._batch_record_menu,
                                        kind="ghost", width=96, height=32)
        self._btn_batch_set = T.RButton(bar, "批量设置 ▾", command=self._batch_set,
                                        kind="ghost", width=96, height=32)
        self._btn_detail = T.RButton(bar, "隐藏详情", command=self._toggle_detail,
                                     kind="text", width=76, height=32)
        self._btn_refresh = T.RButton(bar, "立即检查", command=self.on_force_check,
                                      kind="ghost", width=84, height=32)
        # 从右往左 pack（视觉顺序：立即检查 / 批量录制 / 批量设置 / 隐藏详情）
        for w in (self._btn_detail, self._btn_batch_set, self._btn_batch_rec, self._btn_refresh):
            w.pack(side="right", padx=(8, 0))
        # 批量录制弹出菜单
        self._batch_rec_menu = tk.Menu(self.root, tearoff=0, font=T.font(10))
        self._batch_rec_menu.add_command(label="对选中主播开始录制",
                                         command=lambda: self._manual_record_selected(True))
        self._batch_rec_menu.add_command(label="对选中主播停止录制",
                                         command=lambda: self._manual_record_selected(False))
        self._batch_rec_menu.add_separator()
        self._batch_rec_menu.add_command(label="对选中主播立即检查",
                                         command=self._check_selected)
        self._batch_rec_menu.add_command(label="勾选全部（当前筛选）", command=self._select_all_page)
        self._batch_rec_menu.add_command(label="清空勾选", command=self._clear_checked)

        body = tk.Frame(page, bg=T.BG)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 4))
        # 左右可拖动分栏：表格 | 详情
        self._paned = ttk.PanedWindow(body, orient="horizontal")
        self._paned.pack(fill="both", expand=True)

        # 表格卡片
        card = T.Card(self._paned)
        self._paned.add(card, weight=1)
        self._table_card = card
        inner = card.inner
        tabs = tk.Frame(inner, bg=T.CARD)
        tabs.pack(fill="x", padx=16, pady=(10, 0))
        self._tab_buttons: Dict[str, Dict[str, Any]] = {}
        tab_defs = [("all", "全部"), ("live", "直播中"), ("recording", "录制中"),
                    ("queue", "排队"), ("error", "错误"), ("offline", "离线")]
        for key, label in tab_defs:
            frm = tk.Frame(tabs, bg=T.CARD, cursor="hand2")
            frm.pack(side="left", padx=(0, 18))
            lbl = tk.Label(frm, text=label, bg=T.CARD, fg=T.SUB, font=T.font(10))
            lbl.pack()
            ind = tk.Frame(frm, bg=T.CARD, height=2)
            ind.pack(fill="x", pady=(3, 0))
            for w in (frm, lbl):
                w.bind("<Button-1>", lambda _e, _k=key: self._on_filter(_k))
            self._tab_buttons[key] = {"frame": frm, "label": lbl, "indicator": ind,
                                      "label_text": label}
        right = tk.Frame(tabs, bg=T.CARD)
        right.pack(side="right")
        self._page_info = tk.Label(right, text="", bg=T.CARD, fg=T.MUTED, font=T.font(9))
        self._page_info.pack(side="right", padx=(10, 0))
        tk.Frame(inner, bg="#F1F4F7", height=1).pack(fill="x", pady=(8, 0))

        table_wrap = tk.Frame(inner, bg=T.CARD)
        table_wrap.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        table_wrap.grid_columnconfigure(0, weight=1)
        table_wrap.grid_rowconfigure(0, weight=1)
        cols = ("sel", "state", "name", "room", "last_live", "quality", "duration",
                "size", "path")
        headings = {"sel": "全选", "state": "状态", "name": "主播", "room": "房间号",
                    "last_live": "最近开播", "quality": "清晰度",
                    "duration": "本次时长", "size": "文件大小", "path": "输出路径"}
        self.tree = ttk.Treeview(table_wrap, columns=cols, show="headings",
                                 selectmode="extended")
        widths = {"sel": 48, "state": 84, "name": 200, "room": 110, "last_live": 96,
                  "quality": 130, "duration": 84, "size": 84, "path": 240}
        for c in cols:
            if c == "sel":
                self.tree.heading("sel", text="全选", command=self._toggle_all_visible)
                self.tree.column("sel", width=48, minwidth=40, anchor="center",
                                 stretch=False)
                continue
            self.tree.heading(c, text=headings[c], command=lambda _c=c: self._sort_by(_c))
            self.tree.column(c, width=widths[c], minwidth=60, anchor="w",
                             stretch=(c in ("name", "path")))
        vsb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self._empty_hint = tk.Label(
            table_wrap,
            text="还没有主播\n点击上方「＋ 导入直播间」批量导入（Excel / CSV / 粘贴链接）",
            bg=T.CARD, fg=T.MUTED, font=T.font(10), justify="center")
        self.tree.tag_configure("even", background=T.ZEBRA)
        self.tree.tag_configure("odd", background=T.CARD)
        self.tree.tag_configure("disabled", foreground=T.MUTED)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_detail())
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self._on_context_menu)
        self._ctx_menu = tk.Menu(self.root, tearoff=0, font=T.font(10))
        self._ctx_menu.add_command(label="开始录制", command=lambda: self._manual_record_selected(True, only_selection=True))
        self._ctx_menu.add_command(label="停止录制", command=lambda: self._manual_record_selected(False, only_selection=True))
        self._ctx_menu.add_command(label="立即检查", command=self._check_selected)
        self._ctx_menu.add_command(label="重新启用并检查", command=self._reenable_selected)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="编辑", command=self.on_edit)
        self._ctx_menu.add_command(label="打开目录", command=self.on_open_dir)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="跳转到抖音主页", command=self._open_homepage)
        self._ctx_menu.add_command(label="浏览器打开直播间", command=self._open_live_room)
        self._ctx_menu.add_command(label="查看直播画面", command=self._open_live_preview)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="删除", command=self.on_delete)

        footer = tk.Frame(inner, bg=T.CARD)
        footer.pack(fill="x", padx=16, pady=(4, 8))
        self._list_info = tk.Label(footer, text="", bg=T.CARD, fg=T.MUTED,
                                   font=T.font(9))
        self._list_info.pack(side="right")

        # 右侧详情抽屉
        self._drawer = tk.Frame(self._paned, bg=T.BG, width=292)
        self._paned.add(self._drawer, weight=0)
        self._drawer.pack_propagate(False)
        dcard = T.Card(self._drawer)
        dcard.pack(fill="both", expand=True)
        d = dcard.inner
        top = tk.Frame(d, bg=T.CARD)
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text="主播详情", bg=T.CARD, fg=T.TEXT, font=T.font(11, True),
                 anchor="w").pack(side="left")
        self._btn_close_drawer = T.RButton(top, "×", command=self._toggle_detail, kind="text",
                                           width=26, height=24, bold=False, bg_parent=T.CARD)
        self._btn_close_drawer.pack(side="right")
        self._cover = tk.Canvas(d, width=246, height=132, bg=T.CARD, highlightthickness=0,
                                cursor="hand2")
        self._cover.pack(padx=14, pady=(0, 8))
        self._cover.bind("<Button-1>", self._on_cover_click)
        self._detail_name = tk.Label(d, text="", bg=T.CARD, fg=T.TEXT, font=T.font(12, True),
                                     anchor="w", wraplength=240, justify="left")
        self._detail_name.pack(fill="x", padx=14)
        self._detail_sub = tk.Label(d, text="", bg=T.CARD, fg=T.SUB, font=T.font(9),
                                    anchor="w", wraplength=240, justify="left")
        self._detail_sub.pack(fill="x", padx=14, pady=(2, 6))
        self._detail_pills = tk.Frame(d, bg=T.CARD)
        self._detail_pills.pack(fill="x", padx=14)
        self._detail_rows: Dict[str, tk.Label] = {}
        info = tk.Frame(d, bg=T.CARD)
        info.pack(fill="x", padx=14, pady=(8, 0))
        for key in ("房间号", "最近开播", "本次时长", "文件大小", "本次清晰度", "分组", "排队原因"):
            r = tk.Frame(info, bg=T.CARD)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=key, bg=T.CARD, fg=T.MUTED, font=T.font(9), anchor="w").pack(side="left")
            val = tk.Label(r, text="—", bg=T.CARD, fg=T.TEXT, font=T.mono(9), anchor="e")
            val.pack(side="right")
            self._detail_rows[key] = val
        self._detail_dir = tk.Label(d, text="", bg=T.CARD, fg=T.SUB, font=T.font(8),
                                    anchor="w", wraplength=240, justify="left")
        self._detail_dir.pack(fill="x", padx=14, pady=(6, 0))
        btns = tk.Frame(d, bg=T.CARD)
        btns.pack(fill="x", padx=14, pady=(8, 0))
        T.RButton(btns, "打开目录", command=self.on_open_dir, kind="primary", width=100,
                  height=30, bg_parent=T.CARD).pack(side="left")
        T.RButton(btns, "直播画面", command=self._open_live_preview, kind="ghost", width=94,
                  height=30, bg_parent=T.CARD).pack(side="left", padx=(8, 0))
        T.RButton(btns, "抖音主页", command=self._open_homepage, kind="ghost", width=94,
                  height=30, bg_parent=T.CARD).pack(side="left", padx=(8, 0))
        tk.Label(d, text="最近文件", bg=T.CARD, fg=T.SUB, font=T.font(9, True),
                 anchor="w").pack(fill="x", padx=14, pady=(10, 2))
        self._detail_files = tk.Label(d, text="—", bg=T.CARD, fg=T.SUB, font=T.font(8),
                                      anchor="w", justify="left", wraplength=240)
        self._detail_files.pack(fill="x", padx=14)

        # 日志条
        self._log_strip = tk.Frame(page, bg=T.BG)
        self._log_strip.pack(fill="x", padx=24, pady=(0, 4))
        self._log_last = tk.Label(self._log_strip, text="", bg=T.BG, fg=T.SUB, font=T.font(9),
                                  anchor="w")
        self._log_last.pack(side="left", fill="x", expand=True)
        self._btn_log_toggle = T.RButton(self._log_strip, "展开日志", command=self._toggle_log,
                                         kind="text", width=84, height=26, bold=False)
        self._btn_log_toggle.pack(side="right")
        self._log_panel = tk.Frame(page, bg=T.CARD, highlightbackground=T.LINE,
                                   highlightthickness=1)
        self._log_text = tk.Text(self._log_panel, height=8, bd=0, bg="#1F2937", fg="#D1D5DB",
                                 insertbackground="#D1D5DB", font=T.mono(9), wrap="none",
                                 state="disabled", padx=10, pady=6)
        self._log_text.pack(fill="both", expand=True)
        self._log_text.tag_configure("info", foreground="#D1D5DB")
        self._log_text.tag_configure("warning", foreground="#FBBF24")
        self._log_text.tag_configure("error", foreground="#F87171")


    # ---------------- 录制记录页 ----------------
    def _build_page_history(self) -> None:
        page = self._new_page("history")
        self._history_days = tk.StringVar(value="7")
        head = tk.Frame(page, bg=T.BG)
        head.pack(fill="x", padx=24, pady=(16, 10))
        box = tk.Frame(head, bg=T.BG)
        box.pack(side="left")
        tk.Label(box, text="录制记录", bg=T.BG, fg=T.TEXT, font=T.font(20, True),
                 anchor="w").pack(anchor="w")
        tk.Label(box, text="每场录制的开始/结束/大小/状态", bg=T.BG, fg=T.SUB,
                 font=T.font(10), anchor="w").pack(anchor="w", pady=(2, 0))
        right = tk.Frame(head, bg=T.BG)
        right.pack(side="right")
        tk.Label(right, text="最近", bg=T.BG, fg=T.SUB, font=T.font(10)).pack(side="left")
        cb = ttk.Combobox(right, textvariable=self._history_days,
                          values=["1", "3", "7", "30", "90"], state="readonly", width=6)
        cb.pack(side="left", padx=(6, 10))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_history())
        T.RButton(right, "刷新", command=self._refresh_history, kind="ghost", width=76,
                  height=32).pack(side="left", padx=(0, 8))
        T.RButton(right, "主播活跃度", command=self._show_activity, kind="ghost",
                  width=110, height=32).pack(side="left", padx=(0, 8))
        T.RButton(right, "补转历史 MP4", command=self._remux_legacy, kind="ghost",
                  width=130, height=32).pack(side="left", padx=(0, 8))
        T.RButton(right, "导出诊断包", command=self._export_diagnostics, kind="primary",
                  width=120, height=32).pack(side="left")

        card = T.Card(page)
        card.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        inner = card.inner
        cols = ("time", "name", "room", "quality", "duration", "size", "status", "path")
        headings = {"time": "开始时间", "name": "主播", "room": "房间号", "quality": "清晰度",
                    "duration": "时长", "size": "大小", "status": "状态", "path": "文件"}
        self._history_tree = ttk.Treeview(inner, columns=cols, show="headings")
        widths = {"time": 150, "name": 180, "room": 120, "quality": 130, "duration": 80,
                  "size": 90, "status": 90, "path": 320}
        for c in cols:
            self._history_tree.heading(c, text=headings[c])
            self._history_tree.column(c, width=widths[c], anchor="w",
                                      stretch=(c in ("name", "path")))
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self._history_tree.yview)
        self._history_tree.configure(yscrollcommand=vsb.set)
        self._history_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6, padx=(0, 6))
        self._history_hint = tk.Label(inner, text="", bg=T.CARD, fg=T.MUTED, font=T.font(9))
        self._history_hint.pack(side="bottom", anchor="w", padx=12, pady=(0, 6))

    # ---------------- 运行日志页 ----------------
    def _build_page_logs(self) -> None:
        page = self._new_page("logs")
        head = tk.Frame(page, bg=T.BG)
        head.pack(fill="x", padx=24, pady=(16, 10))
        box = tk.Frame(head, bg=T.BG)
        box.pack(side="left")
        tk.Label(box, text="运行日志", bg=T.BG, fg=T.TEXT, font=T.font(20, True),
                 anchor="w").pack(anchor="w")
        tk.Label(box, text="按天保存 · 保留天数可在设置中调整", bg=T.BG, fg=T.SUB,
                 font=T.font(10), anchor="w").pack(anchor="w", pady=(2, 0))
        right = tk.Frame(head, bg=T.BG)
        right.pack(side="right")
        self._log_file_var = tk.StringVar()
        self._log_file_combo = ttk.Combobox(right, textvariable=self._log_file_var,
                                            values=[], state="readonly", width=28)
        self._log_file_combo.pack(side="left", padx=(0, 8))
        self._log_file_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_log_file())
        T.RButton(right, "打开日志目录", command=self._open_log_dir, kind="ghost",
                  width=120, height=32).pack(side="left", padx=(0, 8))
        T.RButton(right, "导出诊断包", command=self._export_diagnostics, kind="primary",
                  width=120, height=32).pack(side="left")

        card = T.Card(page)
        card.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        self._log_view = tk.Text(card.inner, bd=0, bg="#1F2937", fg="#D1D5DB",
                                 insertbackground="#D1D5DB", font=T.mono(9), wrap="none",
                                 state="disabled", padx=12, pady=8)
        vsb = ttk.Scrollbar(card.inner, orient="vertical", command=self._log_view.yview)
        self._log_view.configure(yscrollcommand=vsb.set)
        self._log_view.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        vsb.pack(side="right", fill="y", pady=8, padx=(0, 8))

    # ---------------- 设置页 ----------------
    def _build_page_settings(self) -> None:
        page = self._new_page("settings")
        head = tk.Frame(page, bg=T.BG)
        head.pack(fill="x", padx=24, pady=(16, 10))
        box = tk.Frame(head, bg=T.BG)
        box.pack(side="left")
        tk.Label(box, text="设置", bg=T.BG, fg=T.TEXT, font=T.font(20, True),
                 anchor="w").pack(anchor="w")
        tk.Label(box, text="修改后点「保存设置」生效；带 * 的项影响长时间挂机稳定性",
                 bg=T.BG, fg=T.SUB, font=T.font(10), anchor="w").pack(anchor="w", pady=(2, 0))
        T.RButton(head, "保存设置", command=self._save_settings, kind="primary",
                  width=110, height=34).pack(side="right")

        body = tk.Frame(page, bg=T.BG)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=T.BG, width=180)
        left.grid(row=0, column=0, sticky="ns")
        left.grid_propagate(False)
        self._settings_nav: Dict[str, tk.Label] = {}
        for group, _fields in SETTINGS_GROUPS:
            lbl = tk.Label(left, text=group, bg=T.BG, fg="#4B5563", font=T.font(11),
                           anchor="w", padx=12, pady=9, cursor="hand2")
            lbl.pack(fill="x", pady=1)
            lbl.bind("<Button-1>", lambda _e, _g=group: self._render_settings(_g))
            self._settings_nav[group] = lbl

        card = T.Card(body)
        card.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        self._settings_card = card
        self._settings_form = tk.Frame(card.inner, bg=T.CARD)
        self._settings_form.pack(fill="both", expand=True, padx=28, pady=20)
        self._settings_hint = tk.Label(card.inner, text="", bg=T.CARD, fg=T.MUTED,
                                       font=T.font(9), anchor="w")
        self._settings_hint.pack(side="bottom", fill="x", padx=28, pady=(0, 10))

    # ---------------- 关于页 ----------------
    def _build_page_about(self) -> None:
        page = self._new_page("about")
        head = tk.Frame(page, bg=T.BG)
        head.pack(fill="x", padx=24, pady=(16, 10))
        tk.Label(head, text="关于", bg=T.BG, fg=T.TEXT, font=T.font(20, True),
                 anchor="w").pack(side="left")
        card = T.Card(page)
        card.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        text = tk.Text(card.inner, bd=0, bg=T.CARD, fg=T.TEXT, font=T.font(10),
                       wrap="word", padx=24, pady=20)
        text.pack(fill="both", expand=True)
        text.insert("1.0", (
            "抖音直播录制工具 · 本地自用版 v%s\n\n" % __version__ +
            "• 只在本机运行，无需登录抖音账号，不上传任何数据。\n"
            "• 录制链路：抖音直播间接口取流 → ffmpeg -c copy 直录 → 可选转 MP4。\n"
            "• 支持：1000+ 直播间长期挂机监控、开播自动录制、断流重连、卡死看门狗、\n"
            "  分段录制、磁盘保留、过短录制自动删除、群机器人通知、批量导入与批量操作。\n"
            "• 协议层：src/dy_api.py（只读公开直播信息，遇到接口变更只需改这一处）。\n"
            "• 版权：录制内容版权归主播/平台所有，仅供个人学习与本地备份，请勿传播或商用。\n\n"
            "配置文件：config.json / watchlist.json（同目录）\n"
            "日志目录：logs/（按天保存，可导出诊断包）\n"
            "运行记录：data/history.db\n"
        ))
        text.configure(state="disabled")

    # ---------------- 页面切换 ----------------
    def _select_page(self, key: str) -> None:
        for name, page in self._pages.items():
            if name == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for name, nav in self._nav_items.items():
            nav.set_active(name == key)
        if key == "history":
            self._refresh_history()
        elif key == "logs":
            self._refresh_log_list()
        elif key == "settings":
            self._render_settings(self._settings_group)

    # ==================================================================
    # 监控表格
    # ==================================================================
    @staticmethod
    def _status_key(row: Dict[str, Any]) -> str:
        if row.get("recording"):
            return "recording"
        if row.get("reconnecting"):
            return "reconnecting"
        st = str(row.get("status") or "")
        if row.get("is_live") or st == "直播中":
            return "live"
        if "错误" in st or st == "error" or row.get("error"):
            return "error"
        if "排队" in st or row.get("queued") or row.get("waiting"):
            return "queue"
        if "未开播" in st or "离线" in st or st == "offline":
            return "offline"
        if "停止" in st or st == "stopped":
            return "stopped"
        return "idle"

    def _tab_counts(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        counts = {"all": len(rows), "live": 0, "recording": 0, "queue": 0, "error": 0,
                  "offline": 0}
        for r in rows:
            st = self._status_key(r)
            if st == "recording":
                counts["recording"] += 1
                counts["live"] += 1
            elif st == "live":
                counts["live"] += 1
            if st == "queue":
                counts["queue"] += 1
            if st == "error":
                counts["error"] += 1
            if st == "offline":
                counts["offline"] += 1
        return counts

    def _update_tabs(self, counts: Dict[str, int]) -> None:
        for key, ref in self._tab_buttons.items():
            active = key == self._filter
            ref["label"].configure(text="%s %d" % (ref["label_text"], counts.get(key, 0)),
                                   fg=T.PRIMARY_DARK if active else T.SUB,
                                   font=T.font(10, active))
            ref["indicator"].configure(bg=T.PRIMARY if active else T.CARD)

    def _on_filter(self, key: str) -> None:
        self._filter = key
        self._page = 0
        self._refresh_table(force=True)

    def _on_search(self) -> None:
        self._search = self._search_var.get().strip().lower()
        self._page = 0
        self._refresh_table(force=True)

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = False
        self._refresh_table(force=True)

    def _flip_page(self, delta: int) -> None:
        self._page = max(0, self._page + delta)
        self._render_rows(self._visible)

    def _select_all_page(self) -> None:
        """勾选当前列表中的全部主播（不再分页，即全部；筛选时为本筛选结果）。"""
        ids = list(self.tree.get_children())
        self._checked.update(str(i) for i in ids)
        self.tree.selection_set(ids)
        for i in ids:
            try:
                self.tree.set(i, "sel", "☑")
            except Exception:
                pass
        self._update_list_info()

    def _clear_checked(self) -> None:
        self._checked.clear()
        try:
            self.tree.selection_set(())
        except Exception:
            pass
        for i in self.tree.get_children():
            try:
                self.tree.set(i, "sel", "☐")
            except Exception:
                pass
        self._update_list_info()

    def _update_list_info(self, total: Optional[int] = None) -> None:
        """更新底部「共 N 个 · 已勾选 M 个」提示。"""
        try:
            if total is None:
                total = len(self.tree.get_children())
            checked_n = sum(1 for i in self._checked
                            if self.watchlist.get(i) is not None)
            all_n = int(getattr(self, "_all_count", total) or total)
            suffix = "" if total == all_n else "（筛选自 %d 个）" % all_n
            self._list_info.configure(
                text="共 %d 个主播%s · 已勾选 %d 个（滚动条/滚轮查看全部）"
                     % (total, suffix, checked_n))
        except Exception:
            pass

    def _on_tree_click(self, event) -> Optional[str]:
        """点击「勾选」列切换勾选；点行其它区域保持普通选择。"""
        try:
            if self.tree.identify("region", event.x, event.y) != "cell":
                return None
            col = self.tree.identify_column(event.x)
            if col != "#1":
                return None
            rid = self.tree.identify_row(event.y)
            if not rid:
                return None
            if rid in self._checked:
                self._checked.discard(rid)
            else:
                self._checked.add(rid)
            self.tree.set(rid, "sel", "☑" if rid in self._checked else "☐")
            return "break"
        except Exception:
            return None

    def _toggle_all_visible(self) -> None:
        ids = [str(i) for i in self.tree.get_children()]
        if ids and all(i in self._checked for i in ids):
            for i in ids:
                self._checked.discard(i)
        else:
            self._checked.update(ids)
        for i in ids:
            try:
                self.tree.set(i, "sel", "☑" if i in self._checked else "☐")
            except Exception:
                pass
        self._update_list_info()

    def _match_search(self, row: Dict[str, Any]) -> bool:
        if not self._search:
            return True
        hay = " ".join(str(row.get(k) or "") for k in ("name", "room", "group", "title")).lower()
        return self._search in hay

    def _apply_filter(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for r in rows:
            if not self._match_search(r):
                continue
            st = self._status_key(r)
            if self._filter == "all":
                out.append(r)
            elif self._filter == "live":
                if st in ("recording", "live"):
                    out.append(r)
            elif self._filter == "recording":
                if st == "recording":
                    out.append(r)
            elif self._filter == "queue":
                if st == "queue":
                    out.append(r)
            elif self._filter == "error":
                if st == "error":
                    out.append(r)
            elif self._filter == "offline":
                if st == "offline":
                    out.append(r)
        return out

    def _sort_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        col = self._sort_col
        if not col:
            return rows
        order = {"recording": 0, "live": 1, "queue": 2, "reconnecting": 3, "error": 4,
                 "idle": 5, "stopped": 6, "offline": 7}
        if col == "state":
            key = lambda r: (0 if r.get("recording") else 1, order.get(self._status_key(r), 8))
        elif col == "name":
            key = lambda r: str(r.get("name") or "")
        elif col == "room":
            key = lambda r: str(r.get("room") or "")
        elif col == "last_live":
            key = lambda r: int(r.get("last_live_at") or 0)
        elif col == "quality":
            key = lambda r: str(r.get("quality") or "")
        elif col == "duration":
            key = lambda r: float(r.get("duration_sec") or 0)
        elif col == "size":
            key = lambda r: float(r.get("size_bytes") or 0)
        else:
            key = lambda r: str(r.get("path") or "")
        return sorted(rows, key=key, reverse=self._sort_desc)

    def _row_values(self, row: Dict[str, Any]) -> tuple:
        status = self._status_key(row)
        text, _fg, _bg = T.STATUS_PILL.get(status, T.STATUS_PILL["idle"])
        if not row.get("enabled", True):
            text = "已停用"
        state = "● " + text
        eff_quality = row.get("effective_quality") or row.get("quality") or "origin"
        quality = quality_label(eff_quality)
        if row.get("degraded"):
            quality += "（已降档）"
        dur = fmt_duration(row.get("duration_sec") or row.get("duration"))
        size = fmt_size(row.get("file_size") or row.get("size_bytes"))
        path = (row.get("output_path") or row.get("path") or row.get("out_dir") or "")
        rid = str(row.get("id") or "")
        sel = "☑" if rid in self._checked else "☐"
        room = row.get("web_rid") or row.get("room") or ""
        if not room or str(room).startswith("http") or str(room) == row.get("name"):
            room = "待解析"
        last_ts = int(row.get("last_live_at") or 0)
        from .activity import format_ago as _fmt_ago
        last_text = _fmt_ago(last_ts)
        return (sel, state, row.get("name") or "未命名", room, last_text,
                quality, dur, size, path)

    def _refresh_table(self, force: bool = False) -> None:
        try:
            rows = self.manager.snapshot()
        except Exception as exc:  # pragma: no cover
            LOG.warning("读取状态失败: %s", exc)
            rows = []
        self._state_rows = {r.get("id"): r for r in rows}
        valid_ids = set(self._state_rows)
        if self._checked - valid_ids:
            self._checked = {i for i in self._checked if i in valid_ids}
        counts = self._tab_counts(rows)
        self._update_tabs(counts)
        filtered = self._sort_rows(self._apply_filter(rows))
        self._visible = filtered
        self._render_rows(filtered)
        # 若详情抽屉因重建被重置，但选中行仍在，则恢复详情
        try:
            if self._selected_ids() and self._detail_name.cget("text") in ("", "未选择主播"):
                self._refresh_detail()
        except Exception:
            pass
        total = len(filtered)
        try:
            if total == 0:
                if not rows:
                    self._empty_hint.configure(
                        text="还没有主播\n点击上方「＋ 导入直播间」批量导入（Excel / CSV / 粘贴链接）")
                else:
                    self._empty_hint.configure(text="没有符合当前筛选条件的主播")
                self._empty_hint.place(in_=self.tree, relx=0.5, rely=0.45,
                                       anchor="center")
            else:
                self._empty_hint.place_forget()
        except Exception:
            pass
        # 分页已移除：单一滚动列表；底部显示总数与勾选数
        self._all_count = len(rows)
        self._update_list_info(total)
        rec_n = sum(1 for r in rows if r.get("recording"))
        queue_n = counts.get("queue", 0)
        running = self.manager.is_running()
        self._monitor_sub.configure(
            text="%s · 共 %d 个直播间 · 录制 %d · 排队 %d" % (
                "监控运行中" if running else "监控未启动", len(rows), rec_n, queue_n))
        self._btn_start.set_enabled(not running)
        self._btn_stop.set_enabled(running)
        self._status_left.configure(
            text=("● 运行正常" if running else "○ 监控未启动"))
        self._side_status.configure(text="● 运行正常" if running else "○ 监控未启动",
                                    fg=T.OK if running else T.MUTED)
        if running and self._monitor_started_at:
            st = {}
            try:
                st = self.manager.stats()
            except Exception:
                st = {}
            extra = ""
            breaker = str(st.get("breaker_state") or "")
            if breaker and breaker not in ("closed", "正常", "ok", ""):
                remain = int(st.get("breaker_pause_remaining") or 0)
                extra = " · 熔断中" + ((" %ds" % remain) if remain else "")
            short = (" · 过短删除 %d" % self._short_deleted) if self._short_deleted else ""
            self._status_right.configure(
                text="已运行 %s · 录制 %d 路 · 排队 %d%s%s" % (
                    self._elapsed_text(), rec_n, queue_n, short, extra))
        else:
            self._status_right.configure(text="协议层：%s" % self._api_name())

    def _elapsed_text(self) -> str:
        sec = int(time.time() - self._monitor_started_at)
        h, m = sec // 3600, (sec % 3600) // 60
        return ("%d 天 %d 小时" % (h // 24, h % 24)) if h >= 24 else ("%d 小时 %d 分" % (h, m))

    def _api_name(self) -> str:
        try:
            return self.manager.checker.describe() or "dy_api"
        except Exception:
            return "dy_api"

    def _render_rows(self, rows: List[Dict[str, Any]]) -> None:
        # 不再分页：所有（筛选后的）主播一次性放进表格，用滚动条/滚轮查看
        page_rows = rows
        ids = [r.get("id") for r in page_rows]
        existing = list(self.tree.get_children())
        # 排序/状态变化会导致整表重建，必须先记住选中行，重建后恢复，
        # 否则详情抽屉会每隔几秒“自动回到未选择主播”。
        prev_selected = [str(i) for i in self.tree.selection()]
        prev_view = self.tree.yview()
        if existing != ids:
            self.tree.delete(*existing)
            for i, row in enumerate(page_rows):
                rid = row.get("id")
                tags = ["even" if i % 2 else "odd"]
                if not row.get("enabled", True):
                    tags.append("disabled")
                self.tree.insert("", "end", iid=rid, values=self._row_values(row), tags=tuple(tags))
            self._value_cache = {r.get("id"): self._row_values(r) for r in page_rows}
            keep = [i for i in prev_selected if i in ids]
            if keep:
                try:
                    self.tree.selection_set(keep)
                    self.tree.focus(keep[0])
                except Exception:
                    pass
                try:
                    self.tree.yview_moveto(prev_view[0])
                except Exception:
                    pass
        else:
            for row in page_rows:
                rid = row.get("id")
                vals = self._row_values(row)
                if self._value_cache.get(rid) != vals:
                    self.tree.item(rid, values=vals)
                    self._value_cache[rid] = vals
        # 分页已移除

    def _selected_ids(self) -> List[str]:
        """当前高亮行（详情用）。"""
        try:
            return [str(i) for i in self.tree.selection()]
        except Exception:
            return []

    def _target_ids(self) -> List[str]:
        """批量操作对象：优先勾选框，其次高亮行。"""
        checked = [i for i in self._checked if self.watchlist.get(i) is not None]
        return checked or self._selected_ids()

    def _refresh_detail(self) -> None:
        sel = self._selected_ids()
        if not sel:
            self._detail_name.configure(text="未选择主播")
            self._detail_sub.configure(text="在上方列表点击一行查看详情")
            for w in self._detail_pills.winfo_children():
                w.destroy()
            for k, lbl in self._detail_rows.items():
                lbl.configure(text="—")
            self._detail_dir.configure(text="")
            self._detail_files.configure(text="—")
            self._draw_cover(None)
            return
        row = self._state_rows.get(sel[0])
        if not row:
            return
        self._detail_name.configure(text=row.get("name") or "未命名")
        status = self._status_key(row)
        text, fg, bg = T.STATUS_PILL.get(status, T.STATUS_PILL["idle"])
        self._detail_sub.configure(
            text="%s · %s%s" % (row.get("title") or "—", text,
                                " · " + fmt_duration(row.get("duration_sec")) if row.get("recording") else ""))
        for w in self._detail_pills.winfo_children():
            w.destroy()
        T.Pill(self._detail_pills, text, fg, bg, bg_parent=T.CARD).pack(side="left", padx=(0, 6))
        eff_q = row.get("effective_quality") or row.get("quality") or "origin"
        T.Pill(self._detail_pills, quality_label(eff_q) + ("（降档）" if row.get("degraded") else ""),
               T.PRIMARY_DARK, T.SEL, bg_parent=T.CARD).pack(side="left")
        for k, lbl in self._detail_rows.items():
            if k == "房间号":
                rid = str(row.get("web_rid") or "").strip()
                lbl.configure(text=rid if rid and not rid.startswith("http") else "待解析")
            elif k == "最近开播":
                from .activity import format_ago as _fmt_ago
                ts = int(row.get("last_live_at") or 0)
                lbl.configure(text=_fmt_ago(ts),
                              fg=T.WARN if not ts else T.TEXT)
            elif k == "本次时长":
                lbl.configure(text=fmt_duration(row.get("duration_sec")))
            elif k == "文件大小":
                lbl.configure(text=fmt_size(row.get("file_size") or row.get("size_bytes")))
            elif k == "本次清晰度":
                eff = row.get("effective_quality") or row.get("quality") or "origin"
                txt = quality_label(eff)
                if row.get("degraded"):
                    txt += "（断流自动降档）"
                lbl.configure(text=txt, fg=T.WARN if row.get("degraded") else T.TEXT)
            elif k == "排队原因":
                lbl.configure(text=str(row.get("queue_reason") or "—"),
                               fg=T.WARN if row.get("queue_reason") else T.TEXT)
            elif k == "分组":
                grp = row.get("group")
                if not grp:
                    try:
                        it = self.watchlist.get(row.get("id"))
                        grp = getattr(it, "group", "") if it else ""
                    except Exception:
                        grp = ""
                lbl.configure(text=str(grp or "默认"))
        out_dir = self._item_dir(row.get("id"))
        self._detail_dir.configure(text=("输出目录：" + out_dir) if out_dir else "")
        self._detail_files.configure(text=self._recent_files_text(out_dir))
        self._draw_cover(row)

    PREVIEW_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    def _is_live_row(self, row: Dict[str, Any]) -> bool:
        try:
            if row.get("recording") or row.get("is_live"):
                return True
            return str(row.get("status") or "") in ("live", "recording", "queued")
        except Exception:
            return False

    def _cover_base(self) -> None:
        cv = self._cover
        cv.delete("all")
        w, h = 246, 132
        for i in range(h):
            t = i / float(h)
            r = int(34 + (79 - 34) * t)
            g = int(181 + (141 - 181) * t)
            b = int(166 + (247 - 166) * t)
            cv.create_line(0, i, w, i, fill="#%02X%02X%02X" % (r, g, b))

    def _draw_cover(self, row: Optional[Dict[str, Any]]) -> None:
        """详情封面：开播中显示实时画面（ffmpeg 抓帧），未开播显示占位。"""
        cv = self._cover
        self._cover_base()
        if not row:
            cv.create_text(123, 66, text="选择主播查看直播画面", fill="#FFFFFF",
                           font=T.font(10))
            return
        rid = str(row.get("id") or "")
        cached = self._cover_cache.get(rid)
        if cached and self._is_live_row(row):
            _png, ts, photo = cached
            try:
                cv.create_image(123, 66, image=photo)
                cv.create_text(238, 6, text=time.strftime("%H:%M:%S", time.localtime(ts)),
                               anchor="ne", fill="#FFFFFF", font=T.font(8))
                return
            except Exception:
                pass
        if self._is_live_row(row):
            cv.create_text(123, 66, text="正在获取直播画面…", fill="#FFFFFF", font=T.font(10))
            self._ensure_cover(row)
        else:
            cv.create_text(123, 66, text="当前未开播", fill="#FFFFFF", font=T.font(10))
            cv.create_text(123, 88, text="开播后自动显示直播画面", fill="#EAF7F4", font=T.font(8))
        cv.create_text(200, 118, text=(row.get("name") or "")[:10], anchor="e",
                       font=T.font(9), fill="#FFFFFF")

    def _preview_ffmpeg(self) -> str:
        try:
            ok, path, _info = ffmpeg_util.check_ffmpeg(self.cfg.get_str("ffmpeg_path"))
            return path if ok else ""
        except Exception:
            return ""

    def _ensure_cover(self, row: Dict[str, Any]) -> None:
        rid = str(row.get("id") or "")
        if not rid or not self._detail_visible:
            return
        cached = self._cover_cache.get(rid)
        if cached and time.time() - cached[1] < 8:
            return
        if rid in self._cover_pending:
            return
        url = str(row.get("stream_url") or "")
        self._cover_pending.add(rid)
        threading.Thread(target=self._cover_worker, args=(rid, url),
                         name="gui-cover", daemon=True).start()

    def _cover_worker(self, rid: str, url: str) -> None:
        try:
            if not url:
                url = self.manager.preview_url(rid)
            if not url:
                raise RuntimeError("当前没有可用的直播流")
            png = self._capture_frame(url, width=492)
            self._cover_results.put((rid, png, ""))
        except Exception as exc:
            self._cover_results.put((rid, "", str(exc)))

    def _drain_cover_results(self) -> None:
        """UI 线程定时消费抓帧结果（避免跨线程操作 tkinter）。"""
        while True:
            try:
                rid, png, err = self._cover_results.get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            if png:
                self._on_cover_ready(rid, png)
            else:
                self._on_cover_failed(rid, err)

    def _on_cover_ready(self, rid: str, png: str) -> None:
        self._cover_pending.discard(rid)
        try:
            photo = tk.PhotoImage(file=png)
            if photo.width() > 260:
                photo = photo.subsample(2, 2)
        except Exception:
            return
        self._cover_cache[rid] = (png, time.time(), photo)
        sel = self._selected_ids()
        if self._detail_visible and sel and sel[0] == rid:
            self._draw_cover(self._state_rows.get(rid))

    def _on_cover_failed(self, rid: str, msg: str) -> None:
        self._cover_pending.discard(rid)
        LOG.debug("直播画面抓取失败 %s: %s", rid, msg)
        sel = self._selected_ids()
        if sel and sel[0] == rid:
            try:
                self._cover.create_text(123, 92, text="画面获取失败，可点「预览」用播放器打开",
                                        fill="#FFE9E0", font=T.font(8))
            except Exception:
                pass

    def _capture_frame(self, url: str, width: int = 492) -> str:
        ff = self._preview_ffmpeg()
        if not ff:
            raise RuntimeError("未找到 ffmpeg")
        out_dir = Path(tempfile.gettempdir()) / "dyrec_preview"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / ("frame_%d_%d.png" % (abs(hash(url)) % 100000000,
                                              int(time.time() * 1000)))
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
               "-user_agent", self.PREVIEW_UA, "-referer", "https://live.douyin.com/",
               "-i", url, "-frames:v", "1", "-vf", "scale=%d:-1" % width,
               "-f", "image2", str(out)]
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        try:
            subprocess.run(cmd, timeout=25, creationflags=flags,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RuntimeError("抓取画面超时")
        if not out.exists() or out.stat().st_size <= 0:
            raise RuntimeError("抓取画面失败")
        return str(out)

    def _current_row(self) -> Optional[Dict[str, Any]]:
        sel = self._selected_ids()
        return self._state_rows.get(sel[0]) if sel else None

    def _open_homepage(self) -> None:
        row = self._current_row() or {}
        url = str(row.get("homepage") or "")
        if not url:
            messagebox.showinfo("提示", "该主播没有主页链接（列表里填写的是房间号或昵称）",
                                parent=self.root)
            return
        webbrowser.open(url)

    def _open_live_room(self) -> None:
        row = self._current_row() or {}
        url = str(row.get("live_url") or row.get("homepage") or "")
        if not url:
            messagebox.showinfo("提示", "暂无可打开的地址（可能还没解析到房间号）",
                                parent=self.root)
            return
        webbrowser.open(url)

    def _on_cover_click(self, _e=None) -> None:
        self._open_live_preview()

    def _open_live_preview(self) -> None:
        """独立窗口：用 ffmpeg 管道解码直播流，连续实时播放（约 1-3 秒延迟）。"""
        row = self._current_row()
        if row is None:
            messagebox.showinfo("提示", "请先选择一个主播", parent=self.root)
            return
        if self._preview_win is not None and self._preview_win.winfo_exists():
            self._preview_win.lift()
            return
        rid = str(row.get("id") or "")
        name = row.get("name") or "主播"
        win = tk.Toplevel(self.root)
        win.title("直播画面（实时） - %s" % name)
        win.geometry("860x610")
        win.configure(bg=T.BG)
        win.transient(self.root)
        center_on_parent(win, self.root)
        self._preview_win = win
        canvas = tk.Canvas(win, width=800, height=450, bg="#000000",
                          highlightthickness=0)
        canvas.pack(padx=20, pady=(16, 6))
        status = tk.Label(win, text="正在连接直播流…", bg=T.BG, fg=T.SUB,
                          font=T.font(9))
        status.pack()
        bar = tk.Frame(win, bg=T.BG)
        bar.pack(pady=10)
        state: Dict[str, Any] = {"stop": False, "proc": None, "after": None,
                                 "photo": None, "q": queue.Queue(maxsize=3),
                                 "frames": 0}

        def ui(fn) -> None:
            try:
                self.root.after(0, fn)
            except Exception:
                pass

        def stop_proc() -> None:
            proc = state.get("proc")
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        def reader() -> None:
            ff = self._preview_ffmpeg()
            if not ff:
                ui(lambda: status.configure(text="未找到 ffmpeg，无法实时播放"))
                return
            try:
                url = self.manager.preview_url(rid)
            except Exception:
                url = ""
            if not url:
                ui(lambda: status.configure(text="当前没有可用的直播流（可能未开播）"))
                return
            ui(lambda: status.configure(text="正在缓冲直播流…"))
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-user_agent", self.PREVIEW_UA, "-referer", "https://live.douyin.com/",
                   "-fflags", "nobuffer", "-flags", "low_delay",
                   "-analyzeduration", "1000000", "-probesize", "500000",
                   "-i", url, "-an", "-vf", "scale=800:-2", "-r", "8",
                   "-f", "image2pipe", "-vcodec", "ppm", "pipe:1"]
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL,
                                        creationflags=flags)
            except Exception as exc:
                msg = str(exc)
                ui(lambda: status.configure(text="启动播放失败：%s" % msg))
                return
            state["proc"] = proc
            out = proc.stdout

            def read_line() -> bytes:
                try:
                    return out.readline() if out else b""
                except Exception:
                    return b""

            def read_exact(n: int) -> bytes:
                buf = b""
                while len(buf) < n and not state["stop"]:
                    try:
                        chunk = out.read(n - len(buf)) if out else b""
                    except Exception:
                        chunk = b""
                    if not chunk:
                        break
                    buf += chunk
                return buf

            try:
                while not state["stop"]:
                    magic = read_line()
                    if not magic:
                        break
                    if magic.strip() != b"P6":
                        continue
                    dims = read_line().split()
                    if len(dims) < 2:
                        break
                    try:
                        w, h = int(dims[0]), int(dims[1])
                    except ValueError:
                        break
                    read_line()  # maxval
                    raw = read_exact(w * h * 3)
                    if len(raw) < w * h * 3 or state["stop"]:
                        break
                    frame = (b"P6\n" + ("%d %d\n" % (w, h)).encode("ascii")
                             + b"255\n" + raw)
                    try:
                        state["q"].put_nowait(frame)
                    except queue.Full:
                        try:
                            state["q"].get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            state["q"].put_nowait(frame)
                        except queue.Full:
                            pass
                    state["frames"] = int(state["frames"]) + 1
            except Exception:
                pass
            finally:
                stop_proc()
                state["proc"] = None
                if not state["stop"]:
                    ui(lambda: status.configure(
                        text="直播流已结束或无法播放（可点「用播放器打开」）"))

        def poll() -> None:
            if state["stop"] or not win.winfo_exists():
                return
            frame = None
            try:
                while True:
                    frame = state["q"].get_nowait()
            except queue.Empty:
                pass
            if frame is not None:
                try:
                    photo = tk.PhotoImage(data=frame)
                    state["photo"] = photo
                    canvas.delete("all")
                    canvas.create_image(400, 225, image=photo)
                    status.configure(text="实时直播画面（约 1-3 秒延迟）")
                except Exception:
                    pass
            state["after"] = self.root.after(40, poll)

        def close() -> None:
            state["stop"] = True
            if state["after"]:
                try:
                    self.root.after_cancel(state["after"])
                except Exception:
                    pass
            stop_proc()
            self._preview_win = None
            try:
                win.destroy()
            except Exception:
                pass

        T.RButton(bar, "抖音主页", command=self._open_homepage, kind="ghost",
                  width=96, height=32, bg_parent=T.BG).pack(side="left", padx=4)
        T.RButton(bar, "浏览器打开直播间", command=self._open_live_room,
                  kind="primary", width=150, height=32, bg_parent=T.BG).pack(
            side="left", padx=4)
        T.RButton(bar, "用播放器打开", command=self._preview_external, kind="ghost",
                  width=110, height=32, bg_parent=T.BG).pack(side="left", padx=4)
        T.RButton(bar, "关闭", command=close, kind="text", width=70, height=32,
                  bg_parent=T.BG).pack(side="left", padx=4)
        def on_destroy(event) -> None:
            if event.widget is win:
                state["stop"] = True
                stop_proc()

        win.bind("<Destroy>", on_destroy, add="+")
        win.protocol("WM_DELETE_WINDOW", close)
        threading.Thread(target=reader, daemon=True).start()
        poll()

    def _item_dir(self, item_id: Optional[str]) -> str:
        try:
            item = self.watchlist.get(item_id) if item_id else None
            if item is None:
                return ""
            from .model import render_output_dir
            return str(render_output_dir(
                item, self.cfg.get_str("output_template"),
                self.cfg.output_root))
        except Exception as exc:
            LOG.debug("计算主播目录失败: %s", exc)
            try:
                return str(self.cfg.output_root)
            except Exception:
                return ""

    def _recent_files_text(self, out_dir: str) -> str:
        if not out_dir:
            return "—"
        try:
            files = sorted([p for p in Path(out_dir).rglob("*") if p.is_file()],
                           key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        except Exception:
            return "—"
        if not files:
            return "—"
        return "\n".join("%s  %s" % (f.name[:34], fmt_size(f.stat().st_size)) for f in files)


    # ==================================================================
    # 交互动作
    # ==================================================================
    def _emit_ui(self, text: str, level: str = "info") -> None:
        """把一条消息同时写到日志条与日志面板。"""
        ts = time.strftime("%H:%M:%S")
        self._log_last.configure(text="%s  %s" % (ts, text))
        self._append_log_line("%s  %s" % (ts, text), level)

    def _append_log_line(self, line: str, level: str = "info") -> None:
        tb = self._log_text
        tb.configure(state="normal")
        tb.insert("end", line + "\n", level)
        lines = int(tb.index("end-1c").split(".")[0])
        if lines > 2000:
            tb.delete("1.0", "%d.0" % (lines - 2000))
        tb.see("end")
        tb.configure(state="disabled")

    def on_add(self) -> None:
        dlg = ItemDialog(self.root, None, "添加主播")
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        item = self.manager.add_item(r["text"], name=r["name"],
                                     auto_record=r["auto_record"], quality=r["quality"],
                                     output_template=r["output_template"])
        if item is not None:
            if not r.get("enabled", True):
                item.enabled = False
                self.manager.update_item(item)
            self._emit_ui("已添加主播：%s" % item.display_name)
        self._refresh_table(force=True)

    def _selected_item(self) -> Optional[WatchItem]:
        sel = self._selected_ids()
        if not sel:
            return None
        return self.watchlist.get(sel[0])

    def on_edit(self) -> None:
        item = self._selected_item()
        if item is None:
            messagebox.showinfo("提示", "请先在列表中选择一个主播", parent=self.root)
            return
        dlg = ItemDialog(self.root, item, "编辑主播")
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        item.name = r["name"] or item.name
        item.auto_record = r["auto_record"]
        item.enabled = r["enabled"]
        item.quality = r["quality"]
        item.output_template = r["output_template"]
        new_text = r["text"].strip()
        if new_text and new_text != (item.target or ""):
            parsed = parse_target_text(new_text)
            item.target = new_text
            item.web_rid = parsed.get("web_rid") or item.web_rid
            item.url = parsed.get("url") or item.url
        self.manager.update_item(item)
        self._emit_ui("已更新主播：%s" % item.display_name)
        self._refresh_table(force=True)

    def on_delete(self) -> None:
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择要删除的主播", parent=self.root)
            return
        names = []
        for i in ids:
            it = self.watchlist.get(i)
            names.append(it.display_name if it else i)
        if not messagebox.askyesno("删除确认",
                                   "确定删除选中的 %d 个主播？\n%s" % (
                                       len(ids), "\n".join(names[:10]) + ("\n…" if len(names) > 10 else "")),
                                   parent=self.root):
            return
        for i in ids:
            self.manager.remove_item(i)
        self._emit_ui("已删除 %d 个主播" % len(ids))
        self._refresh_table(force=True)

    def on_start(self) -> None:
        ok, msg = self.manager.start()
        if ok:
            self._monitor_started_at = time.time()
            self._emit_ui(msg)
        else:
            messagebox.showwarning("无法开始监控", msg, parent=self.root)
        self._refresh_table(force=True)

    def _shutdown_wait(self) -> float:
        try:
            return float(max(0, self.cfg.get_int("shutdown_wait_seconds")))
        except Exception:
            return 600.0

    def on_stop(self) -> None:
        if not self.manager.is_running():
            return
        self._btn_stop.set_enabled(False, "停止中…")
        self._emit_ui("正在停止监控：先停止录制并转换 MP4…")
        wait = self._shutdown_wait()

        def worker() -> None:
            done = True
            try:
                done = self.manager.stop(wait=True, timeout=wait)
                if not done:
                    done = self.manager.wait_finalize(wait)
            except Exception as exc:  # pragma: no cover
                LOG.warning("停止监控异常: %s", exc)
            try:
                self.root.after(0, lambda: self._after_stop(done))
            except Exception:
                pass

        threading.Thread(target=worker, name="gui-stop", daemon=True).start()

    def _after_stop(self, done: bool = True) -> None:
        self._monitor_started_at = 0.0
        self._btn_stop.set_enabled(False, "停止监控")
        if done:
            self._emit_ui("监控已停止，录像已转成 MP4")
        else:
            self._emit_ui("监控已停止，但仍有录像在后台转换，请等待完成后再关闭软件",
                          "warning")
        self._refresh_table(force=True)

    def _check_ffmpeg_on_start(self) -> None:
        """启动时检测 ffmpeg；缺失则提示下载/手动选择。"""
        if self._closing:
            return
        try:
            ok, path, info = ffmpeg_util.check_ffmpeg(self.cfg.get_str("ffmpeg_path"))
        except Exception:
            ok, path, info = False, "", "ffmpeg 检测异常"
        if ok:
            self._emit_ui("ffmpeg 就绪：%s" % path)
            return
        hint = ""
        try:
            hint = ffmpeg_util.missing_ffmpeg_hint()
        except Exception:
            hint = "%s\n\n可通过「设置 → ffmpeg 路径」手动指定，或点下方按钮下载。" % info
        self._emit_ui("未找到 ffmpeg：录制功能不可用，请先下载或指定路径", "warning")
        dlg = FfmpegMissingDialog(self.root, self.cfg, hint)
        self.root.wait_window(dlg)

    def on_force_check(self) -> None:
        try:
            self.manager.force_check()
            self._emit_ui("已请求立即检查（下一轮立即执行）")
        except Exception as exc:
            self._emit_ui("立即检查失败：%s" % exc, "warning")

    def on_open_dir(self) -> None:
        """在资源管理器中打开该主播的录像目录（不是软件目录）。"""
        item = self._selected_item()
        if item is None:
            messagebox.showinfo("提示", "请先在列表中选择一个主播", parent=self.root)
            return
        target = self._item_dir(item.id)
        try:
            root = self.cfg.output_root
        except Exception:
            root = None
        if not target:
            target = str(root or "")
        if not target:
            messagebox.showwarning("打开目录失败", "未配置输出目录", parent=self.root)
            return
        p = Path(target)
        try:
            if not p.exists():
                # 默认模板是 主播/日期：日期目录可能还没建，优先打开已存在的主播目录
                if p.parent.exists():
                    p = p.parent
                else:
                    p.mkdir(parents=True, exist_ok=True)
            if root is not None:
                try:
                    p.resolve().relative_to(Path(root).resolve())
                except Exception:
                    # 只允许打开输出根目录内的路径（防止异常模板指向别处）
                    p = Path(root)
                    p.mkdir(parents=True, exist_ok=True)
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showwarning("打开目录失败", str(exc), parent=self.root)

    def on_double_click(self, _event=None) -> None:
        item = self._selected_item()
        if item is None:
            return
        self._preview_selected()

    def _on_context_menu(self, event) -> None:
        try:
            row = self.tree.identify_row(event.y)
            if row and row not in self.tree.selection():
                self.tree.selection_set(row)
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._ctx_menu.grab_release()
            except Exception:
                pass

    def _manual_record_selected(self, start: bool = True, only_selection: bool = False) -> None:
        # 右键菜单：只对「右键选中的那行」操作，不受勾选框影响；批量按钮：按勾选框
        ids = self._selected_ids() if only_selection else self._target_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择主播（可按住 Ctrl/Shift 多选）", parent=self.root)
            return
        action = "开始录制" if start else "停止录制"

        def worker() -> None:
            ok_n = 0
            queued_n = 0
            messages = []
            for i in ids:
                try:
                    if start:
                        ok, msg = self.manager.manual_record(i)
                    else:
                        ok, msg = self.manager.manual_stop(i)
                except Exception as exc:
                    ok, msg = False, str(exc)
                if ok:
                    ok_n += 1
                    if start and "排队" in msg:
                        queued_n += 1
                else:
                    messages.append(msg)
            # 排队/失败原因也要明确展示，避免「点了开始却只显示成功」的假象
            shown = list(messages)
            if queued_n:
                shown.append("%d 个已加入排队（磁盘/带宽受限，闸门释放后自动开录）" % queued_n)
            for m in shown:
                try:
                    self.root.after(0, lambda m=m: self._emit_ui(m, "warning"))
                except Exception:
                    pass
            msg2 = "%s：成功 %d / 共 %d" % (action, ok_n, len(ids))
            try:
                self.root.after(0, lambda: self._emit_ui(msg2))
                self.root.after(300, lambda: self._refresh_table(force=True))
            except Exception:
                pass

        threading.Thread(target=worker, name="gui-manual", daemon=True).start()
        self._emit_ui("正在对 %d 个主播执行%s…" % (len(ids), action))

    def _reenable_selected(self) -> None:
        """把被自动停用/手动停用的主播重新启用，并请求立即检查。"""
        ids = self._target_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择主播", parent=self.root)
            return
        count = 0
        for i in ids:
            item = self.watchlist.get(i)
            if item is None:
                continue
            item.enabled = True
            try:
                self.manager.update_item(item)
            except Exception:
                try:
                    self.watchlist.update(item)
                except Exception:
                    pass
            count += 1
        try:
            self.watchlist.save()
        except Exception:
            pass
        if count:
            self._emit_ui("已重新启用 %d 个主播，并请求立即检查" % count)
        try:
            self.manager.force_check()
        except Exception:
            pass
        self._refresh_table(force=True)

    def _check_selected(self) -> None:
        ids = self._target_ids()
        if not ids:
            return
        try:
            self.manager.force_check()
            self._emit_ui("已请求立即检查 %d 个主播" % len(ids))
        except Exception as exc:
            self._emit_ui("立即检查失败：%s" % exc, "warning")

    def _batch_record_menu(self) -> None:
        try:
            x = self._btn_batch_rec.winfo_rootx()
            y = self._btn_batch_rec.winfo_rooty() + self._btn_batch_rec.winfo_height()
            self._batch_rec_menu.tk_popup(x, y)
        finally:
            try:
                self._batch_rec_menu.grab_release()
            except Exception:
                pass

    def _batch_set(self) -> None:
        ids = self._target_ids()
        if not ids:
            messagebox.showinfo("提示", "请先选择主播（可按住 Ctrl/Shift 多选）", parent=self.root)
            return
        dlg = BatchSetDialog(self.root, len(ids))
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        field, value = dlg.result.get("field"), dlg.result.get("value")
        changed = 0
        for i in ids:
            item = self.watchlist.get(i)
            if item is None:
                continue
            if field == "auto_record":
                item.auto_record = bool(value)
            elif field == "enabled":
                item.enabled = bool(value)
            elif field == "quality":
                item.quality = str(value)
            elif field == "group":
                item.group = str(value)
            self.manager.update_item(item)
            changed += 1
        self._emit_ui("批量设置完成：%d 个主播" % changed)
        self._refresh_table(force=True)

    def export_watchlist(self) -> None:
        """导出当前全部主播（含分组/画质/开关/模板/备注），供升级后无缝导入。"""
        try:
            from . import export_util
        except Exception as exc:  # pragma: no cover
            messagebox.showerror("导出失败", "导出模块不可用：%s" % exc, parent=self.root)
            return
        default = "主播列表_%s.xlsx" % time.strftime("%Y%m%d_%H%M")
        path = filedialog.asksaveasfilename(
            title="导出主播列表",
            defaultextension=".xlsx",
            initialfile=default,
            filetypes=[("Excel 工作簿", "*.xlsx"), ("CSV 文件", "*.csv")],
            parent=self.root)
        if not path:
            return
        try:
            count = export_util.export_watchlist(self.watchlist, path)
        except Exception as exc:
            self._emit_ui("导出失败：%s" % exc, "error")
            messagebox.showerror("导出失败", "导出失败：%s" % exc, parent=self.root)
            return
        self._emit_ui("已导出 %d 个主播：%s" % (count, path))
        messagebox.showinfo(
            "导出成功",
            ("已导出 %d 个主播。\n\n升级版本后，打开「批量导入」选择这个文件即可恢复全部主播"
             "（名称/房间号/主页链接/分组/画质/自动录制/参与监控/输出模板/备注）。\n\n%s"
             % (count, path)),
            parent=self.root)

    def _batch_import(self) -> None:
        dlg = BatchImportDialog(self.root)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        records = list(r.get("records") or [])
        if not records:
            records = import_util.records_from_lines(r.get("lines") or [])
        # 与已有列表去重
        existing = set()
        for it in self.watchlist.all():
            try:
                key = import_util.target_key(it.web_rid or it.url or it.target)
                if key:
                    existing.add(key)
            except Exception:
                continue
        accepted, dup, invalid = import_util.dedupe(records, existing)
        # 文件里无法识别的行（不是链接/房间号）也算失败
        invalid += int(r.get("file_skipped") or 0)
        # 只有昵称、没有房间号/链接的条目无法监控（会一直查询失败）：
        # 若是本工具导出且标记「参与监控=否」，则恢复为停用条目；否则判为无效跳过。
        usable: List[Dict[str, str]] = []
        name_only = 0
        for rec in accepted:
            try:
                key = import_util.target_key(rec.get("target") or "")
            except Exception:
                key = ""
            if key.startswith("txt:"):
                # 仅昵称条目：保留但强制停用（无法查询直播，不参与监控；升级导入不丢数据）
                fixed = dict(rec)
                fixed["enabled"] = "否"
                usable.append(fixed)
                name_only += 1
                continue
            usable.append(rec)
        accepted = usable
        invalid += name_only
        added = 0
        restored_disabled = 0
        store_log = logging.getLogger("dyrec.storage")
        old_level = store_log.level
        store_log.setLevel(logging.WARNING)  # 批量导入时不逐条刷日志
        for rec in accepted:
            auto = import_util.parse_bool(rec.get("auto_record"), bool(r["auto_record"]))
            quality = import_util.parse_quality(rec.get("quality"), r["quality"])
            template = rec.get("output_template") or ""
            try:
                item = self.watchlist.add_from_text(
                    rec["target"], name=rec.get("name") or "",
                    auto_record=auto, quality=quality,
                    output_template=template, save=False)
            except TypeError:  # 旧版 storage 无 save 参数
                item = self.watchlist.add_from_text(
                    rec["target"], name=rec.get("name") or "",
                    auto_record=auto, quality=quality,
                    output_template=template)
            if item is None:
                continue
            if rec.get("web_rid") and not item.web_rid:
                item.web_rid = rec["web_rid"]
            if rec.get("homepage") and not item.url:
                item.url = rec["homepage"]
            if rec.get("group"):
                item.group = rec["group"]
            elif r.get("group"):
                item.group = r["group"]
            if "enabled" in rec:
                item.enabled = import_util.parse_bool(rec.get("enabled"), True)
            if "auto_record" in rec:
                item.auto_record = auto
            if rec.get("note"):
                item.note = rec["note"]
            if item.enabled is False or import_util.target_key(item.target).startswith("txt:"):
                restored_disabled += 1
            added += 1
        store_log.setLevel(old_level)
        try:
            self.watchlist.save()
            self.manager.flush()
        except Exception as exc:
            self._emit_ui("保存主播列表失败：%s" % exc, "error")
        self._emit_ui("批量导入完成：新增 %d · 重复跳过 %d · 无效 %d（现有 %d）" % (
            added, dup, invalid, len(self.watchlist)))
        if added or dup or invalid:
            LOG.info("导入结果：成功 %d，已存在 %d，失败 %d（仅昵称 %d）",
                     added, dup, invalid, name_only)
            messagebox.showinfo(
                "导入直播间 · 结果",
                ("成功导入：%d 个%s\n"
                 "已存在（重复跳过）：%d 个\n"
                 "失败（无法识别/缺链接）：%d 个%s\n"
                 "\n当前列表共：%d 个"
                 % (added,
                    ("（其中 %d 个仅昵称，已按「停用」保留）" % restored_disabled
                     if restored_disabled else ""),
                    dup,
                    invalid,
                    ("（其中 %d 个只有昵称/缺链接，已按「停用」保留）" % name_only
                     if name_only else ""),
                    len(self.watchlist))),
                parent=self.root)
        self._refresh_table(force=True)

    def _preview_external(self) -> None:
        """用系统默认播放器打开直播流（兜底方案）。"""
        item = self._selected_item()
        if item is None:
            return
        self._emit_ui("正在获取直播流地址…")

        def worker() -> None:
            try:
                st = self.manager.checker.check(item)
                if not st.is_live:
                    self.root.after(0, lambda: self._emit_ui("主播当前未开播，无法预览", "warning"))
                    return
                url = self.manager.checker.choose_url(st)
                if not url:
                    self.root.after(0, lambda: self._emit_ui("未拿到可播放的流地址", "warning"))
                    return
                os.startfile(url)  # type: ignore[attr-defined]
                self.root.after(0, lambda: self._emit_ui("已交给系统播放器预览"))
            except Exception as exc:
                self.root.after(0, lambda: self._emit_ui("预览失败：%s" % exc, "warning"))

        threading.Thread(target=worker, name="gui-preview", daemon=True).start()

    def _toggle_detail(self) -> None:
        self._detail_visible = not self._detail_visible
        if self._detail_visible:
            try:
                self._paned.add(self._drawer, weight=0)
            except Exception:
                pass
            self._btn_detail.set_text("隐藏详情")
        else:
            try:
                self._paned.forget(self._drawer)
            except Exception:
                pass
            self._btn_detail.set_text("显示详情")

    def _toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        if self._log_visible:
            self._log_panel.pack(fill="x", padx=24, pady=(0, 8), before=self._log_strip)
            self._btn_log_toggle.set_text("收起日志")
        else:
            self._log_panel.pack_forget()
            self._btn_log_toggle.set_text("展开日志")

    # ==================================================================
    # 事件与定时刷新
    # ==================================================================
    def _on_manager_event(self, event: RecordEvent) -> None:
        try:
            self._event_q.put_nowait(event)
        except Exception:  # pragma: no cover
            pass

    def _poll_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                ev = self._event_q.get_nowait()
                try:
                    msg = ev.message or ""
                    if getattr(ev, "deleted", False) or "已自动删除" in msg or "过短录制已删除" in msg:
                        self._short_deleted += 1
                except Exception:
                    pass
                text = ev.message or ev.format()
                level = "error" if ev.kind == EventType.ERROR else (
                    "warning" if ev.kind in (EventType.RECONNECT, EventType.DISK) else "info")
                self._emit_ui(text if ev.name else "[%s] %s" % (ev.kind, text), level)
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.root.after(150, self._poll_events)
        except Exception:
            pass

    def _poll_logs(self) -> None:
        if self._closing:
            return
        try:
            while True:
                rec = self._log_q.get_nowait()
                try:
                    msg = rec.getMessage()
                except Exception:
                    msg = str(rec.msg)
                level = rec.levelname.lower()
                tag = "error" if level in ("error", "critical") else (
                    "warning" if level == "warning" else "info")
                ts = time.strftime("%H:%M:%S", time.localtime(rec.created))
                self._append_log_line("%s [%s] %s" % (ts, rec.levelname, msg), tag)
                if tag in ("error", "warning"):
                    self._log_last.configure(text="%s  %s" % (ts, msg))
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.root.after(200, self._poll_logs)
        except Exception:
            pass

    def _refresh_loop(self) -> None:
        if self._closing:
            return
        try:
            self._drain_cover_results()
            self._refresh_table()
            if self._pages.get("logs") is not None and self._pages["logs"].winfo_ismapped():
                self._load_log_file(silent=True)
        except Exception as exc:  # pragma: no cover
            LOG.debug("刷新异常: %s", exc)
        try:
            self.root.after(max(300, self.cfg.get_int("ui_refresh_ms", 1000) or 1000),
                            self._refresh_loop)
        except Exception:
            pass

    # ==================================================================
    # 录制记录页数据
    # ==================================================================
    def _refresh_history(self) -> None:
        try:
            days = int(self._history_days.get())
        except Exception:
            days = 7
        rows: List[Dict[str, Any]] = []
        hint = "运行记录模块尚未就绪（data/history.db 会在录制后自动生成）"
        try:
            from . import history as H  # type: ignore
            fn = None
            for name in ("list_recent_recordings", "list_recordings", "recent_recordings", "query"):
                fn = getattr(H, name, None)
                if fn is not None:
                    break
            if fn is not None:
                try:
                    rows = fn(days=days) or []
                except TypeError:
                    rows = fn(days) or []
                hint = "共 %d 条记录（最近 %d 天）" % (len(rows), days)
        except Exception as exc:
            hint = "运行记录模块不可用：%s" % exc
        self._history_rows = rows
        for iid in self._history_tree.get_children():
            self._history_tree.delete(iid)
        def _ts(v: Any) -> str:
            try:
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(v)))
            except Exception:
                return str(v or "")

        def _status(v: Any, deleted: Any = 0) -> str:
            if deleted:
                return "已删除(过短)"
            m = {"stopped": "已结束", "error": "失败", "recording": "录制中",
                 "live": "直播中", "completed": "已完成", "": ""}
            return m.get(str(v or ""), str(v or ""))

        for r in rows:
            if not isinstance(r, dict):
                continue
            self._history_tree.insert("", "end", values=(
                _ts(r.get("started_at")),
                r.get("anchor") or r.get("name") or "",
                r.get("room_id") or r.get("web_rid") or "",
                quality_label(str(r.get("quality") or "origin")),
                fmt_duration(r.get("duration_sec") or r.get("duration")),
                fmt_size(r.get("size_bytes") or r.get("size")),
                _status(r.get("status"), r.get("deleted")),
                r.get("path") or r.get("file") or "",
            ))
        self._history_hint.configure(text=hint)

    # ==================================================================
    # 运行日志页
    # ==================================================================
    def _log_dir(self) -> Path:
        try:
            return Path(self.cfg.log_dir_path)
        except Exception:
            return Path("logs")

    def _show_activity(self) -> None:
        """主播活跃度：最近开播时间 / 多久没开播 / 从未开播。"""
        try:
            from . import activity
        except Exception as exc:
            messagebox.showerror("不可用", "活跃度模块加载失败：%s" % exc, parent=self.root)
            return
        try:
            rows = activity.collect_activity(
                self.watchlist, self.cfg.output_root,
                self.cfg.get_str("output_template"), backfill=True)
        except Exception as exc:
            messagebox.showerror("统计失败", str(exc), parent=self.root)
            return
        dlg = ActivityDialog(self.root, rows)
        self.root.wait_window(dlg)

    def _remux_legacy(self) -> None:
        """把输出目录中残留的 .flv/.ts 补转成 MP4（旧版本异常退出的遗留文件）。"""
        if self.manager.is_running():
            messagebox.showinfo("提示", "请先停止监控，再补转历史文件（避免动到正在录制的文件）。",
                                parent=self.root)
            return
        try:
            from . import remux_legacy
        except Exception as exc:  # pragma: no cover
            messagebox.showerror("不可用", "补转模块加载失败：%s" % exc, parent=self.root)
            return
        try:
            root = self.cfg.output_root
        except Exception:
            root = None
        if root is None:
            return
        files = remux_legacy.find_sources(Path(root), min_age_seconds=120)
        if not files:
            messagebox.showinfo("没有需要补转的文件",
                                "输出目录里没有发现未转 MP4 的 .flv/.ts 文件。",
                                parent=self.root)
            return
        if not messagebox.askyesno(
                "补转历史 MP4",
                ("发现 %d 个未转 MP4 的录像文件。\n\n转换在后台进行（不转码、只换容器），"
                 "完成后源文件会按「保留原文件」设置决定是否删除。\n\n开始转换？" % len(files)),
                parent=self.root):
            return
        keep_source = bool(self.cfg.get_bool("keep_source"))
        ff = ""
        try:
            ok, ff, _info = ffmpeg_util.check_ffmpeg(self.cfg.get_str("ffmpeg_path"))
            if not ok:
                ff = ""
        except Exception:
            ff = ""
        if not ff:
            messagebox.showerror("缺少 ffmpeg", "未找到可用的 ffmpeg，无法补转。", parent=self.root)
            return
        self._emit_ui("开始补转 %d 个历史录像…" % len(files))

        def worker() -> None:
            def progress(index, total, path, ok, msg):
                try:
                    self.root.after(0, lambda: self._emit_ui(
                        "补转历史 MP4：%d/%d %s" % (index, total, path.name)))
                except Exception:
                    pass
            result = remux_legacy.convert_sources(files, ff, keep_source=keep_source,
                                                  on_progress=progress)
            text = ("补转完成：成功 %d / 失败 %d / 共 %d 个文件"
                    % (result["converted"], result["failed"], result["total"]))
            if result["errors"]:
                text += "\n\n失败示例：\n" + "\n".join(result["errors"][:5])
            try:
                self.root.after(0, lambda: messagebox.showinfo("补转结果", text, parent=self.root))
                self.root.after(0, lambda: self._emit_ui(text))
            except Exception:
                pass

        threading.Thread(target=worker, name="gui-remux-legacy", daemon=True).start()

    def _refresh_log_list(self) -> None:
        d = self._log_dir()
        try:
            files = sorted([p for p in d.glob("*.log") if p.is_file()],
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            files = []
        names = [p.name for p in files]
        self._log_file_combo.configure(values=names)
        if names and self._log_file_var.get() not in names:
            self._log_file_var.set(names[0])
        elif not names:
            self._log_file_var.set("")
        self._load_log_file(silent=True)

    def _load_log_file(self, silent: bool = False) -> None:
        name = self._log_file_var.get()
        path = self._log_dir() / name if name else None
        if path is None or not path.exists():
            if not silent:
                self._log_view.configure(state="normal")
                self._log_view.delete("1.0", "end")
                self._log_view.insert("end", "暂无日志文件（运行后自动生成）")
                self._log_view.configure(state="disabled")
            return
        try:
            data = path.read_bytes()
            text = data.decode("utf-8", "replace")
            tail = "\n".join(text.splitlines()[-2000:])
        except Exception as exc:
            tail = "读取日志失败：%s" % exc
        try:
            pos = float(self._log_view.yview()[0])
        except Exception:
            pos = 1.0
        self._log_view.configure(state="normal")
        self._log_view.delete("1.0", "end")
        self._log_view.insert("end", tail)
        self._log_view.configure(state="disabled")
        if pos < 0.98:
            try:
                self._log_view.yview_moveto(pos)
            except Exception:
                pass
        else:
            self._log_view.see("end")

    def _open_log_dir(self) -> None:
        d = self._log_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showwarning("打开失败", str(exc), parent=self.root)

    def _export_diagnostics(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出诊断包", defaultextension=".zip",
            initialfile="douyin-recorder-diagnostics-%s.zip" % time.strftime("%Y%m%d-%H%M"),
            filetypes=[("ZIP 压缩包", "*.zip")], parent=self.root)
        if not path:
            return
        script = Path(__file__).resolve().parent.parent / "run.py"

        def worker() -> None:
            # 直接在进程内生成（打包版里 sys.executable 是 GUI exe，不能再 spawn run.py）
            try:
                from .cli import export_diagnostics
                out = export_diagnostics(path, self.cfg, self.watchlist,
                                         days=self.cfg.get_int("log_keep_days") or 30)
                ok = out.exists() and out.stat().st_size > 0
                msg = ("诊断包已导出：%s（%.1f KB）" % (out, out.stat().st_size / 1024)
                       if ok else "导出失败：文件未生成")
                try:
                    if ok:
                        self.root.after(0, lambda: messagebox.showinfo(
                            "诊断包已导出", msg, parent=self.root))
                    else:
                        self.root.after(0, lambda: messagebox.showerror(
                            "导出失败", msg, parent=self.root))
                except Exception:
                    pass
            except Exception as exc:
                msg = "导出失败：%s" % exc
                try:
                    self.root.after(0, lambda: messagebox.showerror(
                        "导出失败", msg, parent=self.root))
                except Exception:
                    pass
            try:
                self.root.after(0, lambda: self._emit_ui(msg))
            except Exception:
                pass

        self._emit_ui("正在导出诊断包…")
        threading.Thread(target=worker, name="gui-diag", daemon=True).start()

    # ==================================================================
    # 设置页
    # ==================================================================
    def _cfg_get(self, path: str, default: Any = None) -> Any:
        cur: Any = self._settings_draft
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def _cfg_set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        cur = self._settings_draft
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = value

    def _reload_setting_draft(self) -> None:
        try:
            import copy
            self._settings_draft = copy.deepcopy(dict(self.cfg))
        except Exception:
            self._settings_draft = {}

    def _render_settings(self, group: str) -> None:
        self._collect_settings_group()
        self._settings_group = group
        for name, lbl in self._settings_nav.items():
            active = name == group
            lbl.configure(bg=T.SEL if active else T.BG,
                          fg=T.PRIMARY_DARK if active else "#4B5563",
                          font=T.font(11, active))
        form = self._settings_form
        for w in form.winfo_children():
            w.destroy()
        self._setting_vars = {}
        fields = dict(SETTINGS_GROUPS).get(group, [])
        row = 0
        for key, label, kind, hint in fields:
            if kind == "text":
                tk.Label(form, text=label, bg=T.CARD, fg=T.TEXT, font=T.font(10),
                         anchor="w").grid(row=row, column=0, sticky="nw", padx=(0, 16), pady=(8, 2))
                txt = tk.Text(form, height=4, bd=0, bg="#F8FAFC", fg=T.TEXT, font=T.mono(9),
                              highlightthickness=1, highlightbackground=T.INPUT_LINE,
                              highlightcolor=T.PRIMARY, padx=8, pady=6, wrap="none")
                txt.grid(row=row, column=1, sticky="we", pady=(8, 2))
                hooks = self._cfg_get(key, []) or []
                txt.insert("1.0", "\n".join(str(h) for h in hooks))
                self._setting_vars[key] = ("text", txt)
                row += 1
                continue
            tk.Label(form, text=label, bg=T.CARD, fg=T.TEXT, font=T.font(10),
                     anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 16), pady=6)
            value = self._cfg_get(key, "")
            if kind == "bool":
                sw = T.Switch(form, value=bool(value), bg_parent=T.CARD)
                sw.grid(row=row, column=1, sticky="w", pady=6)
                self._setting_vars[key] = ("bool", sw)
            elif kind == "combo":
                if key == "container":
                    label_map = {v: l for l, v in CONTAINER_CHOICES}
                    values = [l for l, _v in CONTAINER_CHOICES]
                    cur = label_map.get(str(value), values[0])
                elif key == "quality":
                    values = [l for l, _v in QUALITY_CHOICES]
                    cur = quality_label(str(value or "origin"))
                elif key == "output_template":
                    values = [l for l, _v in OUTPUT_TEMPLATE_CHOICES]
                    tmpl = str(value or "{anchor}/{date}").strip()
                    cur = next((lab for lab, v in OUTPUT_TEMPLATE_CHOICES
                                if v != "__custom__" and v == tmpl), "自定义…")
                else:
                    values = ["DEBUG", "INFO", "WARNING", "ERROR"]
                    cur = str(value or "INFO").upper()
                cb = ttk.Combobox(form, values=values, state="readonly",
                                  width=(34 if key == "output_template" else 22))
                cb.set(cur)
                cb.grid(row=row, column=1, sticky="w", pady=6)
                self._setting_vars[key] = ("combo", cb)
            elif kind == "ffmpeg":
                wrap = tk.Frame(form, bg=T.CARD)
                wrap.grid(row=row, column=1, sticky="we", pady=6)
                var = tk.StringVar(value=str(value or ""))
                T.make_entry(wrap, var, 30).pack(side="left", fill="x", expand=True, ipady=4)
                T.RButton(wrap, "检测", command=lambda v=var: self._detect_ffmpeg(v),
                          kind="ghost", width=64, height=30, bg_parent=T.CARD).pack(
                    side="left", padx=(8, 0))
                T.RButton(wrap, "下载 ffmpeg", command=self._open_ffmpeg_download,
                          kind="text", width=100, height=30, bg_parent=T.CARD).pack(
                    side="left", padx=(4, 0))
                self._setting_vars[key] = ("entry", var)
            elif kind in ("dir", "file"):
                wrap = tk.Frame(form, bg=T.CARD)
                wrap.grid(row=row, column=1, sticky="we", pady=6)
                var = tk.StringVar(value=str(value or ""))
                T.make_entry(wrap, var, 34).pack(side="left", fill="x", expand=True, ipady=4)
                T.RButton(wrap, "浏览",
                          command=lambda v=var, k=kind: self._pick_path(v, k),
                          kind="ghost", width=64, height=30, bg_parent=T.CARD).pack(
                    side="left", padx=(8, 0))
                self._setting_vars[key] = ("entry", var)
            elif kind == "str":
                var = tk.StringVar(value=str(value or ""))
                T.make_entry(form, var, 24).grid(row=row, column=1, sticky="w", pady=6,
                                                 ipady=4)
                self._setting_vars[key] = ("entry", var)
            elif kind in ("int", "float"):
                shown = value
                if key in _MINUTE_SETTING_KEYS:
                    shown = _seconds_to_min_text(value or 0)
                var = tk.StringVar(value=str(shown if shown != "" else 0))
                T.make_entry(form, var, 14).grid(row=row, column=1, sticky="w", pady=6,
                                                 ipady=4)
                self._setting_vars[key] = ("entry", var)
            if hint:
                row += 1
                tk.Label(form, text=hint, bg=T.CARD, fg=T.MUTED, font=T.font(8),
                         anchor="w").grid(row=row, column=1, sticky="w", pady=(0, 2))
            row += 1
        form.grid_columnconfigure(1, weight=1)
        self._settings_hint.configure(text="配置文件：%s" % self.cfg.path)

    def _collect_settings_group(self) -> None:
        if not getattr(self, "_setting_vars", None):
            return
        for key, (kind, widget) in self._setting_vars.items():
            try:
                if kind == "text":
                    raw = widget.get("1.0", "end").strip()
                    values = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                    self._cfg_set(key, values)
                elif kind == "bool":
                    self._cfg_set(key, bool(widget.get()))
                elif kind == "combo":
                    val = widget.get()
                    if key == "container":
                        for lab, v in CONTAINER_CHOICES:
                            if lab == val:
                                val = v
                                break
                    elif key == "quality":
                        val = quality_value(val)
                    elif key == "output_template":
                        if val == "自定义…" or val == "__custom__":
                            custom_var = self._setting_vars.get("output_template_custom")
                            raw = custom_var[1].get().strip() if custom_var else ""
                            val = raw or "{anchor}"
                        else:
                            for lab, v in OUTPUT_TEMPLATE_CHOICES:
                                if lab == val and v != "__custom__":
                                    val = v
                                    break
                        val = str(val).replace("\\", "/").strip().strip("/") or "{anchor}"
                    self._cfg_set(key, val)
                else:
                    txt = str(widget.get()).strip()
                    cur = self._cfg_get(key, "")
                    if key in _MINUTE_SETTING_KEYS:
                        self._cfg_set(key, int(round(float(txt or 0) * 60)))
                        continue
                    if isinstance(cur, float) or key == "min_free_gb":
                        self._cfg_set(key, float(txt or 0))
                    elif isinstance(cur, int) or key in (
                            "min_keep_seconds", "log_keep_days", "max_concurrent",
                            "max_total_mbps", "max_ffmpeg_processes", "max_poll_workers",
                            "poll_rate_limit", "poll_interval_live", "poll_interval_warm",
                            "poll_interval_cold", "resolve_workers", "segment_duration",
                            "segment_size_mb", "check_interval", "retry_max",
                            "retry_interval", "stall_check_seconds", "keep_days",
                            "keep_max_files"):
                        self._cfg_set(key, int(float(txt or 0)))
                    else:
                        self._cfg_set(key, txt)
            except Exception:
                continue

    def _save_settings(self) -> None:
        self._collect_settings_group()
        try:
            # 移除只读派生字段
            for k in ("notify",):
                pass
            import copy
            for k, v in self._settings_draft.items():
                if k == "notify" and isinstance(v, dict):
                    self.cfg["notify"] = copy.deepcopy(v)
                else:
                    self.cfg[k] = v
            ok = self.cfg.save()
            disk = {}
            if ok:
                try:
                    disk = self.manager.apply_disk_policy()
                except Exception:
                    disk = {}
            if ok:
                self._emit_ui("设置已保存")
                try:
                    if disk.get("blocked"):
                        messagebox.showwarning(
                            "空间不足，已停止录制",
                            ("设置已保存。\n\n" + str(disk.get("message") or
                             "磁盘剩余空间低于保留值，已立即停止录制并暂停开录。")),
                            parent=self.root)
                    else:
                        messagebox.showinfo("保存成功", "设置已保存并立即生效。",
                                            parent=self.root)
                except Exception:
                    pass
            else:
                self._emit_ui("设置保存失败（请看日志）", "warning")
                try:
                    messagebox.showerror("保存失败", "配置写入失败，请检查目录权限或查看运行日志。",
                                         parent=self.root)
                except Exception:
                    pass
            self._reload_setting_draft()
            self._refresh_table(force=True)
        except Exception as exc:
            self._emit_ui("保存设置失败：%s" % exc, "error")
            try:
                messagebox.showerror("保存失败", "保存设置时出错：%s" % exc, parent=self.root)
            except Exception:
                pass

    def _pick_path(self, var: tk.StringVar, kind: str) -> None:
        if kind == "dir":
            path = filedialog.askdirectory(initialdir=var.get() or str(Path.home()),
                                           parent=self.root)
        else:
            path = filedialog.askopenfilename(
                title="选择 ffmpeg.exe", filetypes=[("可执行文件", "*.exe"),
                                                    ("所有文件", "*.*")],
                parent=self.root)
        if path:
            var.set(path)

    def _detect_ffmpeg(self, var: Optional[tk.StringVar] = None) -> None:
        configured = var.get().strip() if var is not None else self.cfg.get_str("ffmpeg_path")
        try:
            ok, path, info = ffmpeg_util.check_ffmpeg(configured)
        except Exception as exc:
            ok, path, info = False, "", str(exc)
        if ok:
            if var is not None:
                var.set(path)
            self._emit_ui("ffmpeg 检测成功：%s" % path)
        else:
            self._emit_ui("未找到 ffmpeg：%s（可点「下载 ffmpeg」）" % info, "warning")

    def _open_ffmpeg_download(self) -> None:
        try:
            webbrowser.open("https://www.gyan.dev/ffmpeg/builds/")
        except Exception as exc:
            self._emit_ui("打开下载页失败：%s" % exc, "warning")

    # ==================================================================
    # 关闭
    # ==================================================================
    def on_close(self) -> None:
        if self._closing_dialog is not None:
            return
        if self._closing:
            return
        self._closing = True
        self._emit_ui("正在退出：停止录制并转换 MP4…")
        self._show_closing_dialog()
        self._start_close_worker(self._shutdown_wait())

    def _start_close_worker(self, wait: float) -> None:
        self._close_done_event.clear()
        self._close_result = True

        def worker() -> None:
            done = True
            try:
                done = self.manager.stop(wait=True, timeout=wait)
                if not done:
                    done = self.manager.wait_finalize(wait)
            except Exception:
                LOG.warning("退出时停止监控异常", exc_info=True)
            self._close_result = bool(done)
            self._close_done_event.set()

        threading.Thread(target=worker, name="gui-close", daemon=True).start()

    def _show_closing_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("正在退出")
        win.geometry("440x150")
        center_on_parent(win, self.root)
        win.resizable(False, False)
        win.configure(bg=T.BG)
        win.transient(self.root)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        label = tk.Label(win, text="正在停止录制并转换 MP4…\n完成后会自动退出，请稍候。",
                         bg=T.BG, fg=T.TEXT, font=T.font(10), justify="left")
        label.pack(padx=20, pady=(24, 6), anchor="w")
        hint = tk.Label(win, text="", bg=T.BG, fg=T.MUTED, font=T.font(9),
                        justify="left")
        hint.pack(padx=20, anchor="w")
        self._closing_dialog = win
        self._closing_label = label
        self._closing_hint = hint
        self._poll_closing()
        try:
            win.lift()
        except Exception:
            pass

    def _poll_closing(self) -> None:
        win = self._closing_dialog
        if win is None or not win.winfo_exists():
            return
        if self._close_done_event.is_set():
            self._close_done_event.clear()
            self._close_done(self._close_result)
            if self._closing_dialog is not None and win.winfo_exists():
                self.root.after(500, self._poll_closing)
            return
        try:
            n = self.manager.pending_count()
        except Exception:
            n = 0
        try:
            if n > 0:
                self._closing_label.configure(
                    text="正在停止录制并转换 MP4…\n剩余 %d 路，请稍候。" % n)
            else:
                self._closing_label.configure(text="正在收尾…")
            self._closing_hint.configure(
                text="等待上限 %d 秒（设置→录制→退出时等待转换上限）"
                     % int(self._shutdown_wait()))
        except Exception:
            pass
        self.root.after(500, self._poll_closing)

    def _close_done(self, done: bool) -> None:
        if done:
            self._hide_closing_dialog()
            self._destroy()
            return
        n = 0
        try:
            n = self.manager.pending_count()
        except Exception:
            pass
        try:
            again = messagebox.askyesno(
                "仍在转换",
                ("还有 %d 路录制/转换未完成。\n\n继续等待（推荐）？\n"
                 "选择「否」将立即退出，未转换完的文件会保留为 FLV/TS。" % n),
                parent=self._closing_dialog or self.root)
        except Exception:
            again = True
        if again:
            self._start_close_worker(self._shutdown_wait())
        else:
            self._hide_closing_dialog()
            self._destroy()

    def _hide_closing_dialog(self) -> None:
        win = self._closing_dialog
        self._closing_dialog = None
        self._closing_label = None
        try:
            if win is not None and win.winfo_exists():
                win.destroy()
        except Exception:
            pass

    def _destroy(self) -> None:
        try:
            self.manager.flush()
        except Exception:
            pass
        try:
            LOG.removeHandler(self._log_handler)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_gui(config: Config, watchlist: Watchlist) -> int:
    app = RecorderApp(config, watchlist)
    return app.run()
