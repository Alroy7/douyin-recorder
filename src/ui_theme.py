# -*- coding: utf-8 -*-
"""UI 主题与自定义控件（风格 A「青屿」：清新简洁 · 青绿主色）。

- 统一配色/字体/间距/圆角
- 自定义控件：圆角按钮、状态胶囊、导航项、卡片容器、开关
- 高 DPI 适配
所有控件只用标准库 tkinter，无第三方依赖。
"""
from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable, Optional

# ---------------------------------------------------------------- 配色
BG = "#F5F7F9"
CARD = "#FFFFFF"
PRIMARY = "#22B5A6"
PRIMARY_DARK = "#15897D"
PRIMARY_HOVER = "#1EA396"
TEXT = "#111827"
SUB = "#6B7280"
MUTED = "#9CA3AF"
LINE = "#E8EDF1"
INPUT_LINE = "#DDE4EA"
ZEBRA = "#F8FAFB"
SEL = "#E9F7F3"
DANGER = "#C81E1E"
DANGER_HOVER = "#B01818"
WARN = "#B45309"
WARN_BG = "#FEF3C7"
OK = "#0E9F6E"
OK_BG = "#E6F7F1"
ERR = "#B91C1C"
ERR_BG = "#FEE2E2"
OFF = "#6B7280"
OFF_BG = "#F1F3F5"
SIDEBAR_BG = "#FFFFFF"
HOVER = "#F3F6F8"

# 状态 -> (显示文字, 前景, 背景)
STATUS_PILL = {
    "recording": ("录制中", DANGER, "#FDE8E8"),
    "live": ("直播中", OK, OK_BG),
    "offline": ("离线", OFF, OFF_BG),
    "idle": ("未检查", MUTED, OFF_BG),
    "queue": ("排队中", WARN, WARN_BG),
    "reconnecting": ("重连中", WARN, WARN_BG),
    "error": ("错误", ERR, ERR_BG),
    "stopped": ("已停止", OFF, OFF_BG),
}

_FONT_FAMILY: Optional[str] = None
_FONT_MONO: Optional[str] = None


def init_dpi(root: tk.Misc) -> float:
    """开启高 DPI 感知并按 DPI 调整 tk scaling。返回缩放系数。"""
    scale = 1.0
    try:
        if sys.platform == "win32":
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
            try:
                scale = float(ctypes.windll.shcore.GetScaleFactorForDevice(0)) / 100.0
            except Exception:
                dc = ctypes.windll.user32.GetDC(0)
                dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88) or 96
                ctypes.windll.user32.ReleaseDC(0, dc)
                scale = dpi / 96.0
    except Exception:
        scale = 1.0
    try:
        root.tk.call("tk", "scaling", 1.333 * max(1.0, scale))
    except Exception:
        pass
    return max(1.0, scale)


def init_fonts(root: tk.Misc) -> None:
    global _FONT_FAMILY, _FONT_MONO
    families = set(tkfont.families(root))
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "Segoe UI"):
        if name in families:
            _FONT_FAMILY = name
            break
    else:
        _FONT_FAMILY = "TkDefaultFont"
    for name in ("Consolas", "Cascadia Mono", "Courier New"):
        if name in families:
            _FONT_MONO = name
            break
    else:
        _FONT_MONO = "TkFixedFont"


def font(size: int = 10, bold: bool = False) -> tuple:
    return (_FONT_FAMILY or "TkDefaultFont", size, "bold" if bold else "normal")


def mono(size: int = 9) -> tuple:
    return (_FONT_MONO or "TkFixedFont", size)


def setup_styles(root: tk.Misc) -> ttk.Style:
    """配置 ttk 主题（clam 基底 + 明确定义每个控件）。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    root.configure(bg=BG)
    style.configure(".", background=BG, foreground=TEXT, font=font(10))
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("White.TFrame", background=CARD)
    style.configure("Sidebar.TFrame", background=SIDEBAR_BG)
    style.configure("TLabel", background=BG, foreground=TEXT, font=font(10))
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("Sub.TLabel", background=BG, foreground=SUB, font=font(9))
    style.configure("CardSub.TLabel", background=CARD, foreground=SUB, font=font(9))
    style.configure("Title.TLabel", background=BG, foreground=TEXT, font=font(20, True))
    style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=font(11, True))
    style.configure("TButton", font=font(10), padding=(12, 6))
    style.configure("TSeparator", background=LINE)
    style.configure("TEntry", fieldbackground=CARD, foreground=TEXT, borderwidth=0,
                    insertcolor=TEXT, padding=(8, 6))
    style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                    arrowcolor=SUB, padding=(8, 5))
    style.map("TCombobox", fieldbackground=[("readonly", CARD)],
              foreground=[("readonly", TEXT)])
    style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                    rowheight=38, borderwidth=0, font=font(10))
    style.configure("Treeview.Heading", background="#FAFBFC", foreground=SUB,
                    font=font(9), relief="flat", borderwidth=0, padding=(8, 8))
    style.map("Treeview.Heading", background=[("active", "#F3F6F8")])
    style.map("Treeview", background=[("selected", SEL)], foreground=[("selected", TEXT)])
    style.configure("Vertical.TScrollbar", background="#DDE4EA", troughcolor=CARD,
                    borderwidth=0, arrowcolor=SUB, width=10)
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TCheckbutton", background=CARD, foreground=TEXT, font=font(10))
    style.map("TCheckbutton", background=[("active", CARD)])
    style.configure("TRadiobutton", background=CARD, foreground=TEXT, font=font(10))
    style.configure("TSpinbox", fieldbackground=CARD, foreground=TEXT, borderwidth=0)
    return style


# ---------------------------------------------------------------- 自定义控件
def _round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（用多边形近似）。"""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class RButton(tk.Canvas):
    """圆角按钮。kind: primary / ghost / danger / subtle / text。"""

    def __init__(self, parent, text: str, command: Optional[Callable] = None,
                 kind: str = "primary", width: int = 96, height: int = 34,
                 font_size: int = 10, bold: bool = True, bg_parent: str = BG):
        super().__init__(parent, width=width, height=height, highlightthickness=0,
                         bd=0, bg=bg_parent, cursor="hand2")
        self._kind = kind
        # 注意：不要用 self._w（tkinter Widget 内部保存 Tcl 控件名），否则控件失效
        self._cw, self._ch = width, height
        self._command = command
        self._enabled = True
        self._text = text
        self._font = font(font_size, bold)
        self._draw()
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))

    def _colors(self):
        if self._kind == "primary":
            return PRIMARY, PRIMARY_HOVER, "#FFFFFF"
        if self._kind == "danger":
            return DANGER, DANGER_HOVER, "#FFFFFF"
        if self._kind == "ghost":
            return CARD, HOVER, TEXT
        if self._kind == "text":
            return BG, HOVER, PRIMARY_DARK
        return CARD, HOVER, TEXT

    def _draw(self, hover: bool = False) -> None:
        self.delete("all")
        fill, hover_fill, fg = self._colors()
        bg = hover_fill if hover else fill
        border = INPUT_LINE if self._kind == "ghost" else ""
        r = min(9, self._ch // 2)
        _round_rect(self, 0, 0, self._cw, self._ch, r, fill=bg, outline=border, width=1)
        self.create_text(self._cw // 2, self._ch // 2, text=self._text, font=self._font,
                         fill=fg)

    def _hover(self, on: bool) -> None:
        if self._enabled:
            self._draw(on)

    def _on_click(self, _e) -> None:
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, enabled: bool, text: Optional[str] = None) -> None:
        self._enabled = bool(enabled)
        if text is not None:
            self._text = text
        self._draw(False)
        self.configure(cursor="hand2" if enabled else "arrow")

    def set_text(self, text: str) -> None:
        self._text = text
        self._draw(False)


class Pill(tk.Canvas):
    """状态胶囊（圆角小标签）。"""

    def __init__(self, parent, text: str, fg: str, bg: str, bg_parent: str = BG,
                 height: int = 20, padx: int = 9, font_size: int = 9):
        self._text = text
        self._fg = fg
        self._bg = bg
        self._padx = padx
        self._fs = font_size
        w = self._measure(text) + padx * 2
        super().__init__(parent, width=w, height=height, highlightthickness=0, bd=0,
                         bg=bg_parent)
        self._draw()

    def _measure(self, text: str) -> int:
        f = tkfont.Font(font=font(self._fs))
        return f.measure(text) + 2

    def _draw(self) -> None:
        self.delete("all")
        self._h = int(self.cget("height"))
        _round_rect(self, 0, 0, int(self.cget("width")), self._h, self._h // 2,
                    fill=self._bg, outline="")
        self.create_text(int(self.cget("width")) // 2, self._h // 2, text=self._text,
                         font=font(self._fs), fill=self._fg)

    def update_pill(self, text: str, fg: str, bg: str) -> None:
        self._text, self._fg, self._bg = text, fg, bg
        w = self._measure(text) + self._padx * 2
        self.configure(width=w)
        self._draw()


class NavItem(tk.Frame):
    """侧边栏导航项：图标字符 + 文字，选中时青色背景+左侧竖条。"""

    def __init__(self, parent, icon: str, label: str, command: Callable,
                 active: bool = False):
        super().__init__(parent, bg=SIDEBAR_BG, cursor="hand2")
        self._command = command
        self._active = active
        self._indicator = tk.Frame(self, width=3, bg=PRIMARY if active else SIDEBAR_BG)
        self._indicator.pack(side="left", fill="y")
        self._body = tk.Frame(self, bg=SEL if active else SIDEBAR_BG)
        self._body.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=2)
        self._icon = tk.Label(self._body, text=icon, bg=self._body["bg"],
                              fg=PRIMARY_DARK if active else SUB, font=font(11))
        self._icon.pack(side="left", padx=(12, 6), pady=6)
        self._label = tk.Label(self._body, text=label, bg=self._body["bg"],
                               fg=PRIMARY_DARK if active else "#4B5563",
                               font=font(11, active), anchor="w")
        self._label.pack(side="left", fill="x", expand=True, pady=6)
        for w in (self, self._body, self._icon, self._label):
            w.bind("<Button-1>", lambda _e: self._command())
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))

    def _hover(self, on: bool) -> None:
        if self._active:
            return
        bg = HOVER if on else SIDEBAR_BG
        self._body.configure(bg=bg)
        self._icon.configure(bg=bg)
        self._label.configure(bg=bg)

    def set_active(self, active: bool) -> None:
        self._active = active
        bg = SEL if active else SIDEBAR_BG
        self._indicator.configure(bg=PRIMARY if active else SIDEBAR_BG)
        self._body.configure(bg=bg)
        self._icon.configure(bg=bg, fg=PRIMARY_DARK if active else SUB)
        self._label.configure(bg=bg, fg=PRIMARY_DARK if active else "#4B5563",
                              font=font(11, active))


class Card(tk.Frame):
    """白色卡片容器（细边框，圆角靠内容留白体现）。"""

    def __init__(self, parent, padding: int = 0, bg_parent: str = BG):
        super().__init__(parent, bg=CARD, highlightbackground=LINE,
                         highlightcolor=LINE, highlightthickness=1, bd=0)
        self.inner = tk.Frame(self, bg=CARD)
        self.inner.pack(fill="both", expand=True, padx=padding, pady=padding)


class Switch(tk.Canvas):
    """开关控件。"""

    def __init__(self, parent, value: bool = True, command: Optional[Callable] = None,
                 bg_parent: str = CARD):
        super().__init__(parent, width=42, height=22, highlightthickness=0, bd=0,
                         bg=bg_parent, cursor="hand2")
        self._value = bool(value)
        self._command = command
        self._draw()
        self.bind("<Button-1>", self._toggle)

    def _draw(self) -> None:
        self.delete("all")
        fill = PRIMARY if self._value else "#CBD5E1"
        _round_rect(self, 0, 3, 42, 19, 8, fill=fill, outline="")
        cx = 31 if self._value else 11
        self.create_oval(cx - 8, 3, cx + 8, 19, fill="#FFFFFF", outline="")

    def _toggle(self, _e=None) -> None:
        self._value = not self._value
        self._draw()
        if self._command:
            self._command(self._value)

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value = bool(value)
        self._draw()


class CheckBox(tk.Frame):
    """方形勾选框（Canvas 绘制）+ 可选文字，比原生 Checkbutton 更贴合整体风格。"""

    def __init__(self, parent, text: str = "", value: bool = False,
                 command: Optional[Callable] = None, bg_parent: str = CARD):
        try:
            bg = bg_parent or parent.cget("bg")
        except Exception:
            bg = CARD
        super().__init__(parent, bg=bg)
        self._value = bool(value)
        self._command = command
        self._bg = bg
        self.box = tk.Canvas(self, width=20, height=20, bg=bg,
                             highlightthickness=0, bd=0, cursor="hand2")
        self.box.pack(side="left")
        self.label = tk.Label(self, text=text, bg=bg, fg=TEXT, font=font(10),
                              cursor="hand2")
        if text:
            self.label.pack(side="left", padx=(7, 0))
        for w in (self, self.box, self.label):
            w.bind("<Button-1>", self._toggle)
        self._draw()

    def _draw(self) -> None:
        c = self.box
        c.delete("all")
        if self._value:
            _round_rect(c, 1, 1, 19, 19, 6, fill=PRIMARY, outline="")
            c.create_line(5, 10, 9, 14, 15, 6, fill="#FFFFFF", width=2,
                          capstyle="round", joinstyle="round")
        else:
            _round_rect(c, 1, 1, 19, 19, 6, fill="#FFFFFF", outline=INPUT_LINE)

    def _toggle(self, _e=None) -> None:
        self.set(not self._value)
        if self._command:
            try:
                self._command(self._value)
            except Exception:
                pass

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value = bool(value)
        self._draw()


def make_entry(parent, textvariable=None, width: int = 20, bg_parent: str = CARD) -> tk.Entry:
    e = tk.Entry(parent, textvariable=textvariable, width=width, bd=0, relief="flat",
                 bg=CARD, fg=TEXT, insertbackground=TEXT, font=font(10),
                 highlightthickness=1, highlightbackground=INPUT_LINE,
                 highlightcolor=PRIMARY)
    return e


def section_title(parent, text: str, bg_parent: str = CARD) -> tk.Label:
    return tk.Label(parent, text=text, bg=bg_parent, fg=TEXT, font=font(11, True),
                    anchor="w")


def hint_label(parent, text: str, bg_parent: str = CARD, color: str = MUTED) -> tk.Label:
    return tk.Label(parent, text=text, bg=bg_parent, fg=color, font=font(9),
                    anchor="w", justify="left")
