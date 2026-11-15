# -*- coding: utf-8 -*-
"""PyInstaller 运行时钩子：窗口模式（console=False）下的 stdout/stderr 兜底。

PyInstaller 的窗口模式程序如果没有附着控制台（例如双击启动 GUI），
Python 的 sys.stdout/sys.stderr 会是 None，此时任何 print() 都会抛
AttributeError。本钩子按以下顺序兜底，保证程序不会因为打印输出而崩溃：

1. 若父进程留下了有效的标准句柄（管道、重定向文件、控制台），直接复用；
2. 否则尝试 AttachConsole(-1) 附加到调用者控制台，打开 CONOUT$（命令行里能看到输出）；
3. 都失败则使用丢弃输出的空流（双击启动 GUI 的正常情况）。

只处理 stdout/stderr 为 None 的情况；控制台模式（DouyinRecorder-CLI.exe）不受影响。
"""
import os
import sys


class _NullStream(object):
    """丢弃输出的空流（兼容 print/flush/isatty）。"""

    encoding = "utf-8"
    errors = "replace"

    def write(self, text):
        try:
            return len(text)
        except TypeError:
            return 0

    def flush(self):
        pass

    def isatty(self):
        return False

    def close(self):
        pass


def _stream_from_std_handle():
    """把继承来的标准句柄（管道/重定向文件）包装成文本流。"""
    import ctypes
    import io
    import msvcrt

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    except OSError:
        return None
    return io.open(fd, "w", encoding="utf-8", errors="replace", buffering=1)


def _stream_from_console():
    """附加到父进程控制台并打开 CONOUT$（命令行启动时可见输出）。"""
    import ctypes

    ATTACH_PARENT_PROCESS = -1
    if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
        return None
    try:
        return open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return None


def _init_std_streams():
    if sys.platform != "win32":
        return
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        stream = None
        try:
            stream = _stream_from_std_handle()
        except Exception:
            stream = None
        if stream is None:
            try:
                stream = _stream_from_console()
            except Exception:
                stream = None
        setattr(sys, name, stream if stream is not None else _NullStream())
    if sys.stdin is None:
        try:
            sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")
        except OSError:
            sys.stdin = _NullStream()


try:
    _init_std_streams()
except Exception:
    # 运行时钩子不能影响主程序启动
    pass
