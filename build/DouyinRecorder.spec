# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：抖音直播录制工具（本地自用版）—— Windows x64 onedir。

设计要点
--------
1. onedir（目录形式）：启动快（无需每次解压），也是携带 87MB ffmpeg 的合理方式。
2. 双入口：
   * DouyinRecorder.exe     窗口模式（--windowed），双击启动 tkinter GUI；
   * DouyinRecorder-CLI.exe 控制台模式，供命令行/计划任务使用（--check/--once/--list/--cli）。
3. contents_directory="."（PyInstaller 6 的旧式 onedir 布局）：
   所有运行时文件与 exe 平铺在同一目录。这样 src/config.py 中的
   Path(__file__).resolve().parent.parent 会解析到 exe 所在目录（而不是
   _internal/_MEIPASS 临时解包目录），于是 config.json、watchlist.json、
   records/、logs/、data/ 以及 bin/ffmpeg.exe 全部定位到 exe 目录，符合“绿色版”预期。
   （已通过打包后 --check 实测验证。）
4. 随包附带运行时数据：bin/ffmpeg.exe、assets/、config.json 模板（相对路径）。
5. 运行时钩子 rthook_stdout.py：窗口模式在无控制台时 stdout/stderr 为 None，
   钩子做兜底（复用继承句柄 / AttachConsole / 空流），保证 print() 不会把程序打挂。

构建：见 build/build.ps1；也可手动：
    set PYTHONPATH=.pylibs
    python -m PyInstaller --noconfirm --clean --distpath dist --workpath build/work build/DouyinRecorder.spec
"""
import os
from pathlib import Path

# ---- 路径（build.ps1 会设置 DYR_PROJECT_ROOT；直接命令行构建时按 spec 位置推导）----
ROOT = Path(os.environ.get("DYR_PROJECT_ROOT") or Path(SPECPATH).resolve().parent).resolve()
STAGE = ROOT / "build" / "stage"
ICON = ROOT / "build" / "app.ico"
RTHOOK = ROOT / "build" / "rthooks" / "rthook_stdout.py"

# ---- 随包数据：dest 相对 exe 目录（contents_directory="."）----
datas = [
    (str(ROOT / "bin" / "ffmpeg.exe"), "bin"),          # 自带 ffmpeg（87MB）
    (str(ROOT / "assets"), "assets"),                    # 示例文件
    (str(STAGE / "config.json"), "."),                   # 可编辑配置模板（相对路径）
]
_packaging_doc = ROOT / "docs" / "packaging.md"
if _packaging_doc.is_file():
    datas.append((str(_packaging_doc), "docs"))          # 打包说明，方便离线查阅

# ---- 隐式导入（业务模块都是函数内静态 import，正常可分析到；显式列出更稳）----
hiddenimports = [
    "src", "src.cli", "src.capacity", "src.cleanup_dir", "src.checker",
    "src.config", "src.disk",
    "src.dy_api", "src.ffmpeg_util", "src.gui", "src.logger", "src.manager",
    "src.mock_api", "src.model", "src.notify", "src.recorder", "src.remux_legacy",
    "src.storage",
    "tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog",
    "ctypes", "msvcrt", "io",
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT), str(ROOT / ".pylibs")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(RTHOOK)],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

_icon = str(ICON) if ICON.is_file() else None

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="DouyinRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # 窗口模式：双击不弹黑框
    icon=_icon,
    contents_directory=".",        # 旧式 onedir 布局：运行时文件与 exe 同目录
)

exe_cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="DouyinRecorder-CLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                  # 控制台模式：供脚本/计划任务使用
    icon=_icon,
    contents_directory=".",
)

coll = COLLECT(
    exe_gui,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DouyinRecorder",
)
