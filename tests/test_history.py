# -*- coding: utf-8 -*-
"""运行记录 + 诊断包测试（任务 C）。

覆盖：
  1. history.db 写入/读取：record_recording_start/end、run、error、list、stats；
  2. cleanup(days) 只清理超期记录；
  3. 数据库不可写时所有 API 静默降级（不抛异常、返回空/0）；
  4. 多线程并发写入不报错、不丢记录；
  5. export_diagnostics 生成 zip，含预期文件；config.json/日志里的
     cookie、webhook、用户名均已脱敏；watchlist 摘要不含链接/房间号。

运行：python tests/test_history.py
"""
import json
import logging
import os
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.config import Config
from src import history
from src.storage import Watchlist
from src import cli

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("test.history")

BASE = ROOT / "tests" / "tmp" / "history_test"


def _remove_dir(path: Path) -> None:
    history.close_all()
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def fresh_db(name: str = "history.db") -> Path:
    """切换到一个全新的测试数据库。"""
    history.close_all()
    BASE.mkdir(parents=True, exist_ok=True)
    db = BASE / name
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(db) + suffix)
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
    os.environ[history.ENV_DB_PATH] = str(db)
    history.init_db()
    return db


def test_write_read_stats():
    fresh_db("read.db")
    now = time.time()
    a = history.record_recording_start("主播A", "111", "origin",
                                       started_at=now - 30, path=r"C:\Users\Admin\rec\a.flv")
    assert a > 0, "record_recording_start 应返回记录 id"
    assert history.record_recording_end(a, ended_at=now - 10, duration_sec=20,
                                        size_bytes=1024, status="stopped")
    b = history.record_recording_start("主播B", "222", "hd", started_at=now - 3 * 86400)
    history.record_recording_end(b, duration_sec=40, size_bytes=2048, status="stopped")
    c = history.record_recording_start("主播C", "333", started_at=now - 20)
    history.record_recording_end(c, duration_sec=5, size_bytes=100,
                                 status="error", error="断流重连超限")
    old = history.record_recording_start("主播D", started_at=now - 40 * 86400)
    history.record_recording_end(old, duration_sec=100, size_bytes=999)
    run_id = history.record_run_start(version="1.0.0", note="python run.py --history")
    assert run_id > 0
    assert history.record_run_end(run_id, note="done")
    assert history.record_error("recorder:test", "测试错误") > 0

    recent = history.list_recent_recordings(days=7, limit=100)
    anchors = [row["anchor"] for row in recent]
    LOG.info("最近记录: %s", anchors)
    assert "主播D" not in anchors, "超出 7 天的记录不应返回"
    assert anchors[:3] == ["主播C", "主播A", "主播B"], anchors

    stat = history.stats(7)
    today = stat["today"]
    week = stat["week"]
    LOG.info("统计: today=%s week=%s", today, week)
    assert today["count"] == 2, today
    assert abs(today["duration_sec"] - 25) < 0.01, today
    assert today["failed"] == 1, today
    assert week["count"] == 3, week
    assert week["size_bytes"] == 1024 + 2048 + 100, week
    assert week["failed"] == 1

    errors = history.list_recent_errors(days=7)
    assert len(errors) == 1 and "测试错误" in errors[0]["message"]
    return True


def test_cleanup():
    fresh_db("cleanup.db")
    now = time.time()
    old = history.record_recording_start("旧录制", started_at=now - 100 * 86400)
    history.record_recording_end(old, duration_sec=10)
    keep = history.record_recording_start("新录制", started_at=now - 1 * 86400)
    history.record_recording_end(keep, duration_sec=20)
    history.record_run_start(version="0.9", started_at=now - 100 * 86400)
    history.record_error("old", "旧错误", ts=now - 100 * 86400)
    history.record_error("new", "新错误", ts=now - 60)

    deleted = history.cleanup(30)
    LOG.info("清理结果: %s", deleted)
    assert deleted["recordings"] == 1, deleted
    assert deleted["runs"] == 1, deleted
    assert deleted["errors"] == 1, deleted

    rows = history.list_recent_recordings(days=0)
    assert len(rows) == 1 and rows[0]["anchor"] == "新录制", rows
    assert len(history.list_recent_errors(days=0)) == 1

    assert history.cleanup(0) == {"recordings": 0, "runs": 0, "errors": 0}
    return True


def test_write_failure_silent():
    history.close_all()
    BASE.mkdir(parents=True, exist_ok=True)
    blocker = BASE / "blocker_file"
    blocker.write_text("not a dir", encoding="utf-8")
    os.environ[history.ENV_DB_PATH] = str(blocker / "history.db")
    try:
        assert history.record_recording_start("x") == 0
        assert history.record_error("x", "y") == 0
        assert history.record_recording_end(123) is False
        assert history.record_run_start(version="x") == 0
        assert history.list_recent_recordings(7) == []
        assert history.list_recent_errors(7) == []
        stat = history.stats(7)
        assert stat["today"]["count"] == 0 and stat["week"]["count"] == 0
        assert history.cleanup(30)["recordings"] == 0
        LOG.info("数据库不可写时静默降级 OK")
    finally:
        os.environ[history.ENV_DB_PATH] = str(BASE / "dummy.db")
    return True


def test_thread_safety():
    fresh_db("threads.db")
    errors = []

    def worker(index: int) -> None:
        try:
            rec_id = history.record_recording_start(
                "主播%d" % index, started_at=time.time())
            history.record_recording_end(rec_id, duration_sec=index, size_bytes=index * 10)
            history.record_error("thread", "错误 %d" % index)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    rows = history.list_recent_recordings(days=1, limit=100)
    LOG.info("并发写入记录数: %d", len(rows))
    assert len(rows) == 20, len(rows)
    assert len(history.list_recent_errors(days=1, limit=100)) == 20
    return True


def test_export_diagnostics():
    fresh_db("diag.db")
    now = time.time()
    rec_id = history.record_recording_start(
        "主播A", "111", "origin", started_at=now - 60,
        path=r"C:\Users\Admin\rec\a.flv")
    history.record_recording_end(rec_id, duration_sec=60, size_bytes=2048)
    history.record_error("recorder:主播A", r"读取失败 C:\Users\Admin\secret")

    work = BASE / "diag_project"
    _remove_dir(work)
    work.mkdir(parents=True, exist_ok=True)
    logs = work / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "run-2026-11-13.log").write_text(
        "日志行 C:\\Users\\Admin\\secret.txt\n", encoding="utf-8")
    (logs / "other-user.log").write_text("不应被打包\n", encoding="utf-8")

    cfg = Config.load(work / "config.json")
    cfg["cookie"] = "SECRET_COOKIE_VALUE"
    cfg["proxy"] = "http://127.0.0.1:7890"
    cfg["ffmpeg_path"] = r"C:\Users\Admin\bin\ffmpeg.exe"
    cfg["output_dir"] = str(work / "records")
    cfg["log_dir"] = str(logs)
    cfg["notify"]["webhooks"] = ["https://example.com/hook?token=SECRET_TOKEN"]
    cfg.save()

    watchlist = Watchlist(work / "watchlist.json")
    watchlist.clear()
    watchlist.add_from_text("111111111111", name="主播 A")

    dest = BASE / "diag.zip"
    if dest.exists():
        dest.unlink()
    path = cli.export_diagnostics(dest, cfg, watchlist, days=7, log_dir=logs)
    assert path.exists() and path.stat().st_size > 0, path

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        LOG.info("诊断包内容: %s", names)
        for expected in ("README.txt", "config.json", "system.txt", "ffmpeg.txt",
                         "watchlist_summary.txt", "history_stats.json",
                         "logs/run-2026-11-13.log"):
            assert expected in names, "缺少 %s: %s" % (expected, names)
        assert not any("other-user" in name for name in names), names

        config_text = archive.read("config.json").decode("utf-8")
        assert "SECRET_COOKIE_VALUE" not in config_text, "cookie 未脱敏"
        assert "SECRET_TOKEN" not in config_text, "webhook 未脱敏"
        assert "***" in config_text
        assert "\\Users\\Admin" not in config_text, "路径用户名未打码"
        assert "\"ffmpeg_path\"" in config_text

        watch_text = archive.read("watchlist_summary.txt").decode("utf-8")
        assert "111111111111" not in watch_text, "诊断包不应含房间号"
        assert "主播 A" not in watch_text, "诊断包不应含主播名"
        assert "主播总数: 1" in watch_text, watch_text

        log_text = archive.read("logs/run-2026-11-13.log").decode("utf-8")
        assert "\\Users\\Admin" not in log_text, "日志用户名未打码"
        assert "\\Users\\***" in log_text, log_text

        stats = json.loads(archive.read("history_stats.json").decode("utf-8"))
        assert stats["stats"]["week"]["count"] >= 1, stats
        assert "recent_errors" in stats
        assert "Admin" not in json.dumps(stats, ensure_ascii=False), "错误记录里的用户名未打码"

        readme = archive.read("README.txt").decode("utf-8")
        assert "脱敏" in readme
    LOG.info("诊断包生成并脱敏 OK: %s", path)
    return True


TESTS = [
    ("写入/读取/统计", test_write_read_stats),
    ("清理", test_cleanup),
    ("写失败静默", test_write_failure_silent),
    ("并发写入", test_thread_safety),
    ("诊断包导出", test_export_diagnostics),
]


def main():
    failed = []
    for name, fn in TESTS:
        LOG.info("========== 开始: %s ==========", name)
        try:
            fn()
            LOG.info("========== 通过: %s ==========", name)
        except Exception as exc:
            LOG.exception("========== 失败: %s -> %s ==========", name, exc)
            failed.append((name, str(exc)))
    history.close_all()
    print()
    if failed:
        print("FAILED:")
        for name, exc in failed:
            print("  -", name, ":", exc)
        return 1
    print("ALL HISTORY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
