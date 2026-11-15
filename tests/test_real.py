# -*- coding: utf-8 -*-
"""真实抖音直播流录制联调测试。

用法：
    python tests/test_real.py                # 自动发现一个在播房间，录 30 秒
    python tests/test_real.py 55781016966 60 # 指定房间号，录 60 秒

验证：dy_api 取流 -> recorder 直录 FLV -> 文件可被 ffmpeg 解析（时长/大小）。
"""
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.config import Config
from src import ffmpeg_util
from src.checker import LiveChecker
from src.recorder import RoomRecorder
from src.model import EventType

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
LOG = logging.getLogger("test.real")

OUT = ROOT / "tests" / "tmp" / "real_out"
CFG = Config.load(ROOT / "tests" / "tmp" / "test_config_real.json")
FFMPEG = ffmpeg_util.resolve_ffmpeg(CFG.get_str("ffmpeg_path"))[0]


def discover_room():
    """用 dy_api 找一个正在直播的房间号。"""
    import src.dy_api as dy_api
    rooms = dy_api.fetch_recommend(10) or []
    for room in rooms:
        # dy_api.fetch_recommend 返回 LiveStatus 对象；mock 可能返回 dict，这里都兼容
        if isinstance(room, dict):
            rid = str(room.get("web_rid") or room.get("room_id") or "")
            is_live = room.get("is_live", True)
            name = room.get("anchor_name") or room.get("title") or ""
        else:
            rid = str(getattr(room, "room_id", "") or getattr(room, "web_rid", "") or "")
            is_live = bool(getattr(room, "is_live", True))
            name = getattr(room, "anchor_name", "") or getattr(room, "title", "") or ""
        if rid and is_live:
            LOG.info("发现房间: %s %s", rid, name)
            return rid
    return ""


def probe_file(path: Path) -> str:
    """用 ffmpeg -i 读取文件信息（时长等）。"""
    try:
        proc = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-f", "null", "-"],
                              capture_output=True, timeout=120)
        text = (proc.stderr or b"").decode("utf-8", "replace")
        lines = [l.strip() for l in text.splitlines() if "Duration" in l or "Stream #" in l]
        return " | ".join(lines[:4])
    except Exception as exc:
        return "probe 失败: %s" % exc


def main(argv):
    room = argv[1] if len(argv) > 1 else ""
    seconds = int(argv[2]) if len(argv) > 2 else 30
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)

    checker = LiveChecker(CFG)
    if not room:
        room = discover_room()
    if not room:
        print("未找到在播房间，无法进行真实联调")
        return 2
    LOG.info("查询房间 %s 状态...", room)
    st = checker.check(room)
    LOG.info("is_live=%s anchor=%s title=%s error=%s", st.is_live, st.anchor_name, st.title, st.error)
    if st.error:
        print("查询失败: " + st.error)
        return 2
    if not st.is_live:
        print("房间 %s 当前未开播" % room)
        return 2
    url = checker.choose_url(st, "origin")
    LOG.info("流地址: %s", (url or "")[:120])
    if not url:
        print("未取到流地址")
        return 2

    events = []
    def on_event(ev):
        events.append(ev)
        if ev.kind in (EventType.START, EventType.STOPPED, EventType.ERROR, EventType.RECONNECT):
            LOG.info("[事件] %s %s", ev.kind, ev.message)

    base = "real_%s_%s" % (room, time.strftime("%H%M%S"))
    rec = RoomRecorder(
        FFMPEG, url, OUT, base,
        name=st.anchor_name or room, room_id=room,
        container="flv", remux_mp4=False,
        retry_max=3, retry_interval=3,
        stall_check_seconds=30, stall_kill_after=2,
        on_event=on_event,
        is_live_fn=lambda: checker.check(room).is_live,
    )
    assert rec.start(), "启动录制失败"
    LOG.info("开始真实录制 %d 秒...", seconds)
    time.sleep(seconds)
    rec.stop("测试结束")
    ok = rec.join(60)
    stt = rec.status()
    files = [Path(p) for p in stt["files"]]
    files = [f for f in files if f.exists() and f.stat().st_size > 0]
    LOG.info("停止: ok=%s state=%s reason=%s files=%s", ok, stt["state"], stt["stop_reason"],
             [(f.name, f.stat().st_size) for f in files])
    if not files:
        print("真实录制失败：没有生成文件")
        return 1
    total = sum(f.stat().st_size for f in files)
    info = probe_file(files[-1])
    print()
    print("===== 真实录制结果 =====")
    print("房间号  : %s (%s)" % (room, st.anchor_name or "?"))
    print("文件    : %s" % [f.name for f in files])
    print("总大小  : %.2f MB" % (total / 1048576.0))
    print("ffmpeg  : %s" % info)
    print("时长    : %s" % stt["duration"])
    if total < 500 * 1024:
        print("警告：文件过小，可能拉流失败")
        return 1
    print("REAL RECORD TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))