# -*- coding: utf-8 -*-
"""生成构建期资源，并检查构建依赖。

由 build/build.ps1 在调用 PyInstaller 之前执行：
    python build/make_assets.py

做三件事：
1. 检查 .pylibs 里的 PyInstaller 版本（Python 3.13 需要 >= 6.11）和 tkinter 可用性；
2. 从 src/config.py 的 DEFAULTS 生成便携 config.json 模板（相对路径）；
3. 用 Pillow 画一个简单程序图标 build/app.ico（Pillow 不可用则跳过）。

注意：不修改项目根目录的 config.json（那是开发机上的配置）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "stage"
PYLIBS = ROOT / ".pylibs"
for _p in (str(ROOT), str(PYLIBS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MIN_PYINSTALLER = (6, 11)


def check_environment() -> None:
    """检查打包依赖，不满足时以清晰的错误退出。"""
    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        raise SystemExit("当前 Python 缺少 tkinter，无法打包 GUI：%s" % exc)
    try:
        import PyInstaller
    except Exception as exc:
        raise SystemExit(
            "缺少 PyInstaller（.pylibs）。请先执行：\n"
            "  python analysis/pip_fetch.py pyinstaller pyinstaller-hooks-contrib "
            "altgraph packaging pefile pywin32-ctypes\n"
            "原始错误：%s" % exc
        )
    parts = PyInstaller.__version__.split(".")
    try:
        ver = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        ver = (0, 0)
    if ver < MIN_PYINSTALLER:
        raise SystemExit(
            "PyInstaller %s 过低：打包 Python 3.13 需要 >= 6.11，请更新 .pylibs"
            % PyInstaller.__version__
        )
    print("[make_assets] Python %s / PyInstaller %s" % (sys.version.split()[0], PyInstaller.__version__))


def make_config_template() -> Path:
    """从 src/config.py 的 DEFAULTS 生成便携配置模板。

    output_dir / log_dir 写成相对路径（运行时相对 exe 所在目录解析），
    这样把整个目录拷到别的机器/别的盘也能直接用。
    """
    from src.config import DEFAULTS  # noqa: E402  (需要项目根在 sys.path)

    data = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
    data["output_dir"] = "records"
    data["log_dir"] = "logs"
    data["ffmpeg_path"] = ""      # 留空 = 自动找 exe 同级的 bin/ffmpeg.exe
    target = STAGE / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return target


def make_icon() -> Path:
    """用 Pillow 生成一个简单图标（深色底 + 红色录制圆 + 播放三角）。

    不复制原商业软件（逆向对象）的图标，避免版权/商标问题；
    Pillow 不可用时跳过，PyInstaller 会用默认图标。
    """
    target = ROOT / "build" / "app.ico"
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        print("[make_assets] Pillow 不可用，跳过图标生成: %s" % exc)
        return target
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([6, 6, size - 6, size - 6], radius=54, fill=(24, 27, 38, 255))
    draw.ellipse([56, 56, 200, 200], outline=(235, 74, 74, 255), width=14)
    draw.polygon([(112, 88), (112, 168), (180, 128)], fill=(245, 246, 250, 255))
    draw.ellipse([180, 180, 216, 216], fill=(235, 74, 74, 255))
    img.save(target, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                         (64, 64), (128, 128), (256, 256)])
    return target


def main() -> int:
    check_environment()
    cfg = make_config_template()
    print("[make_assets] config 模板: %s (%d bytes)" % (cfg, cfg.stat().st_size))
    ico = make_icon()
    if ico.is_file():
        print("[make_assets] 图标: %s (%d bytes)" % (ico, ico.stat().st_size))
    else:
        print("[make_assets] 图标: 未生成（使用 PyInstaller 默认图标）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
