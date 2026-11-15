# -*- coding: utf-8 -*-
"""单实例锁测试：同进程/跨进程互斥、释放后可获取、残留文件不影响。"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.instance_guard import InstanceGuard

TMP = ROOT / "tests" / "tmp" / "guard"
TMP.mkdir(parents=True, exist_ok=True)


def test_acquire_release():
    p = TMP / "a.lock"
    if p.exists():
        p.unlink()
    g1 = InstanceGuard(p)
    ok, msg = g1.acquire()
    assert ok, msg
    g2 = InstanceGuard(p)
    ok2, msg2 = g2.acquire()
    assert not ok2, "同一进程内第二把锁不应获取成功"
    assert "已在运行" in msg2, msg2
    g2.release()  # 未持有时 release 是安全的
    g1.release()
    ok3, _ = InstanceGuard(p).acquire()
    assert ok3
    print("PASS 同进程互斥/释放后可获取")


def test_cross_process():
    p = TMP / "b.lock"
    if p.exists():
        p.unlink()
    g = InstanceGuard(p)
    ok, msg = g.acquire()
    assert ok, msg
    code = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "from src.instance_guard import InstanceGuard\n"
        "ok, msg = InstanceGuard(r'%s').acquire()\n"
        "print(msg)\n"
        "sys.exit(0 if ok else 5)\n" % (ROOT, p)
    )
    rc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=30).returncode
    assert rc == 5, "子进程不应获取到锁（rc=%s）" % rc
    g.release()
    rc2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30).returncode
    assert rc2 == 0, "释放后子进程应能获取锁（rc=%s）" % rc2
    print("PASS 跨进程互斥/释放后可获取")


def test_stale_file():
    p = TMP / "c.lock"
    p.write_text("999999\n2020-01-01 00:00:00\n", encoding="utf-8")
    g = InstanceGuard(p)
    ok, msg = g.acquire()
    assert ok, "残留文件（进程已死）不应阻止获取: %s" % msg
    g.release()
    print("PASS 残留锁文件可正常接管")


def test_atexit_release():
    p = TMP / "d.lock"
    if p.exists():
        p.unlink()
    code = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "from src.instance_guard import InstanceGuard\n"
        "g = InstanceGuard(r'%s')\n"
        "print(g.acquire()[0])\n" % (ROOT, p)
    )
    subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30)
    ok, msg = InstanceGuard(p).acquire()
    assert ok, "子进程退出后应自动释放锁: %s" % msg
    print("PASS 进程退出自动释放")


def main():
    test_acquire_release()
    test_cross_process()
    test_stale_file()
    test_atexit_release()
    print("ALL INSTANCE GUARD TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
