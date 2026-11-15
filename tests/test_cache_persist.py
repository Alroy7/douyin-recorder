
# -*- coding: utf-8 -*-
"""解析缓存落盘测试（task-12）：temp 文件读写 / TTL 过期 / 损坏容错 / 去抖 / 重启命中 / 环境变量覆盖。

不访问外网：所有解析结果通过 dy_api 内部缓存接口写入，或直接伪造缓存文件。
运行：python tests/test_cache_persist.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 任何测试都不得读写真实 data/resolve_cache.json
TMP = ROOT / "tests" / "tmp" / "cache_persist"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
_ORIG_ENV = os.environ.get("DYREC_RESOLVE_CACHE")
os.environ["DYREC_RESOLVE_CACHE"] = str(TMP / "bootstrap.json")

from src import dy_api  # noqa: E402

SEC = "MS4wLjABAAAA_cache_persist_unit_test_0000000000"
SEC2 = "MS4wLjABAAAA_cache_persist_unit_test_1111111111"
SEC3 = "MS4wLjABAAAA_cache_persist_unit_test_2222222222"


def use_path(path):
    """切换缓存文件路径并清空内存状态（模拟新进程/新会话）。"""
    os.environ["DYREC_RESOLVE_CACHE"] = str(path)
    with dy_api._RESOLVE_LOCK:
        dy_api._sec_uid_cache.clear()
        dy_api._sec_uid_neg_cache.clear()
    dy_api._disk_cache_reset(path=str(path))
    dy_api.reset_http_stats()


def read_file(path):
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


# --------------------------------------------------------------------------- #
# 测试用例
# --------------------------------------------------------------------------- #
def test_schema_and_roundtrip():
    """写入落盘 -> 清内存 -> 重新加载 -> 命中，且文件结构符合 {sec_uid:{web_rid,ts,kind}}。"""
    path = TMP / "roundtrip.json"
    use_path(path)
    dy_api._sec_uid_cache_put(SEC, {
        "web_rid": "111222333", "unique_id": "uid_test", "room_id": "7707592311743925019",
        "nickname": "测试主播", "web_rid_kind": "numeric", "ttl": dy_api.SEC_UID_CACHE_TTL,
    })
    assert dy_api.flush_resolve_cache(), "flush 应成功"
    assert path.exists(), "缓存文件应存在"
    data = read_file(path)
    assert data.get("version") == dy_api.RESOLVE_CACHE_VERSION, data.get("version")
    entry = (data.get("entries") or {}).get(SEC)
    assert isinstance(entry, dict), entry
    assert entry.get("web_rid") == "111222333", entry
    assert entry.get("kind") == "numeric", entry
    assert abs(float(entry.get("ts") or 0) - time.time()) < 30, entry
    assert dy_api.get_http_stats()["cache_disk_saved"] >= 1

    # 模拟重启：清内存 + 重置加载状态，重新加载后直接命中
    with dy_api._RESOLVE_LOCK:
        dy_api._sec_uid_cache.clear()
        dy_api._sec_uid_neg_cache.clear()
    dy_api._disk_cache_reset(path=str(path))
    item = dy_api._sec_uid_cache_get(SEC)
    assert item is not None, "重启后应从磁盘缓存命中"
    assert item.get("web_rid") == "111222333", item
    assert item.get("from_disk") is True
    info = dy_api.get_resolve_cache_info()
    assert info["entries_valid"] == 1, info
    assert info["cache_file"] == os.path.abspath(str(path)), info
    assert dy_api.get_http_stats()["cache_disk_hits"] >= 1


def test_ttl_expiry():
    """加载时丢弃超过 TTL 的条目：数字 web_rid 12h、别名 10min。"""
    path = TMP / "ttl.json"
    now = time.time()
    payload = {
        "version": dy_api.RESOLVE_CACHE_VERSION,
        "updated_at": now,
        "entries": {
            "fresh_numeric": {"web_rid": "100", "ts": now - 60, "kind": "numeric"},
            "old_numeric": {"web_rid": "200", "ts": now - dy_api.SEC_UID_CACHE_TTL - 30, "kind": "numeric"},
            "old_alias": {"web_rid": "300", "ts": now - dy_api.SEC_UID_ALIAS_TTL - 5, "kind": "unique_id_alias"},
            "bad_ts": {"web_rid": "400", "ts": 0, "kind": "numeric"},
            "no_web_rid": {"web_rid": "", "ts": now, "kind": "numeric"},
        },
    }
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp)
    use_path(path)
    info = dy_api.get_resolve_cache_info()
    assert info["entries_valid"] == 1, info
    assert info["entries_valid_numeric"] == 1, info
    assert info["entries_expired"] == 3, info          # old_numeric / old_alias / bad_ts
    assert dy_api._sec_uid_cache_get("fresh_numeric") is not None
    assert dy_api._sec_uid_cache_get("old_numeric") is None
    assert dy_api._sec_uid_cache_get("old_alias") is None


def test_corrupt_file_tolerated():
    """损坏/格式错误文件：只记录 load_error，不抛异常、不命中。"""
    for i, content in enumerate(("not-json{{{", "[]", '{"entries": "bad"}', "")):
        path = TMP / ("corrupt_%d.json" % i)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(content)
        use_path(path)
        info = dy_api.get_resolve_cache_info()   # 不应抛异常
        assert info["entries_total"] == 0, (i, info)
        assert str(info["load_error"]).strip(), (i, info)
        assert dy_api._sec_uid_cache_get(SEC) is None, i


def test_negative_not_persisted():
    """负缓存不落盘；同时会清掉该 sec_uid 可能残留的旧正缓存。"""
    path = TMP / "negative.json"
    use_path(path)
    # 先写一条正缓存
    dy_api._sec_uid_cache_put(SEC, {"web_rid": "999", "web_rid_kind": "numeric"})
    dy_api.flush_resolve_cache()
    assert SEC in read_file(path)["entries"]
    # 再写负缓存 -> 磁盘旧正缓存应被移除，且负缓存本身不落盘
    dy_api._sec_uid_neg_put(SEC, "无直播间")
    dy_api.flush_resolve_cache()
    entries = read_file(path)["entries"]
    assert SEC not in entries, entries
    assert dy_api._sec_uid_neg_get(SEC) is not None, "负缓存应仍在内存"


def test_debounce_writes():
    """写入去抖：短时间多次写缓存只落盘一次；不同批次最多每 interval 一次。"""
    path = TMP / "debounce.json"
    use_path(path)
    old_interval = dy_api.RESOLVE_CACHE_SAVE_INTERVAL
    dy_api.RESOLVE_CACHE_SAVE_INTERVAL = 0.4
    try:
        for i in range(5):
            dy_api._sec_uid_cache_put("debounce_%d" % i, {"web_rid": "50%d" % i, "web_rid_kind": "numeric"})
        assert not path.exists(), "去抖窗口内不应立即落盘"
        time.sleep(0.9)
        assert path.exists(), "去抖窗口后应已落盘"
        first = read_file(path)["entries"]
        assert len(first) == 5, first
        saved_after_first = dy_api.get_http_stats()["cache_disk_saved"]

        # 第二批：窗口内不写；窗口后写
        for i in range(5, 8):
            dy_api._sec_uid_cache_put("debounce_%d" % i, {"web_rid": "50%d" % i, "web_rid_kind": "numeric"})
        time.sleep(0.1)
        assert len(read_file(path)["entries"]) == 5, "第二批在去抖窗口内不应落盘"
        time.sleep(0.9)
        second = read_file(path)["entries"]
        assert len(second) == 8, second
        assert dy_api.get_http_stats()["cache_disk_saved"] > saved_after_first
    finally:
        dy_api.RESOLVE_CACHE_SAVE_INTERVAL = old_interval


def test_save_failure_tolerated():
    """落盘失败（目录不可创建）：只返回 False/不抛异常，修复后能继续保存。"""
    blocker = TMP / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    bad_path = blocker / "cache.json"
    use_path(bad_path)
    dy_api._sec_uid_cache_put(SEC, {"web_rid": "777", "web_rid_kind": "numeric"})
    assert dy_api.flush_resolve_cache() is False, "不可写路径应返回 False"
    # 修复路径：删掉占位文件，创建目录后重试
    blocker.unlink()
    assert dy_api.flush_resolve_cache() is True, "修复后应保存成功"
    assert SEC in read_file(bad_path)["entries"]


def test_restart_new_process_hits_disk():
    """重启场景：当前进程写缓存 -> 新 Python 进程加载后 resolve_target 0 请求直接命中。"""
    path = TMP / "restart.json"
    use_path(path)
    dy_api._sec_uid_cache_put(SEC, {
        "web_rid": "123456789", "unique_id": "uid_restart", "room_id": "7700000000000000000",
        "nickname": "重启测试", "web_rid_kind": "numeric", "ttl": dy_api.SEC_UID_CACHE_TTL,
    })
    assert dy_api.flush_resolve_cache()
    assert path.exists()

    code = "\n".join([
        "import json, sys",
        "sys.path.insert(0, %r)" % str(ROOT),
        "from src import dy_api",
        "r = dy_api.resolve_target('https://www.douyin.com/user/' + %r)" % SEC,
        "s = dy_api.get_http_stats()",
        "print(json.dumps({'web_rid': r.get('web_rid'), 'error': r.get('error'),",
        "                  'requests': s['requests'], 'disk_hits': s['cache_disk_hits']}))",
    ])
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env,
                          cwd=str(ROOT), timeout=120)
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    err = (proc.stderr or b"").decode("utf-8", "replace")
    assert proc.returncode == 0, "子进程失败: rc=%s out=%s err=%s" % (proc.returncode, out[-500:], err[-800:])
    result = json.loads(out.splitlines()[-1])
    assert result["web_rid"] == "123456789", result
    assert result["requests"] == 0, "重启后应完全命中磁盘缓存、0 网络请求: %s" % result
    assert result["disk_hits"] >= 1, result
    assert not result["error"], result


def test_env_override_path():
    """环境变量 DYREC_RESOLVE_CACHE 覆盖缓存文件路径。"""
    custom = TMP / "custom_env.json"
    os.environ["DYREC_RESOLVE_CACHE"] = str(custom)
    assert dy_api._resolve_cache_path() == os.path.abspath(str(custom))
    use_path(custom)
    info = dy_api.get_resolve_cache_info()
    assert info["env_override"] == str(custom), info
    assert info["cache_file"] == os.path.abspath(str(custom)), info


def test_info_reports_memory_and_disk():
    """--check 信息：文件条目数、内存条目数、负缓存不落盘标记。"""
    path = TMP / "info.json"
    use_path(path)
    dy_api._sec_uid_cache_put(SEC, {"web_rid": "555", "web_rid_kind": "numeric"})
    dy_api._sec_uid_neg_put(SEC2, "err")
    dy_api.flush_resolve_cache()
    info = dy_api.get_resolve_cache_info()
    assert info["entries_valid"] == 1, info
    assert info["in_memory_positive"] >= 1, info
    assert info["in_memory_negative"] == 1, info
    assert info["negative_persisted"] is False, info
    assert info["exists"] is True and info["size_bytes"] > 0, info


TESTS = [
    ("落盘读写与重启命中", test_schema_and_roundtrip),
    ("TTL 过期丢弃", test_ttl_expiry),
    ("损坏文件容错", test_corrupt_file_tolerated),
    ("负缓存不落盘", test_negative_not_persisted),
    ("写入去抖", test_debounce_writes),
    ("落盘失败容错", test_save_failure_tolerated),
    ("新进程重启直接命中", test_restart_new_process_hits_disk),
    ("环境变量覆盖路径", test_env_override_path),
    ("--check 信息字段", test_info_reports_memory_and_disk),
]


def main():
    failed = []
    for name, fn in TESTS:
        t0 = time.time()
        try:
            fn()
            print("PASS %s (%.2fs)" % (name, time.time() - t0))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failed.append((name, str(exc)))
            print("FAIL %s -> %s" % (name, exc))
    # 收尾：清内存、恢复环境变量，避免污染真实 data/
    with dy_api._RESOLVE_LOCK:
        dy_api._sec_uid_cache.clear()
        dy_api._sec_uid_neg_cache.clear()
    dy_api._disk_cache_reset()
    if _ORIG_ENV is None:
        os.environ.pop("DYREC_RESOLVE_CACHE", None)
    else:
        os.environ["DYREC_RESOLVE_CACHE"] = _ORIG_ENV
    print()
    if failed:
        print("FAILED:")
        for name, exc in failed:
            print("  -", name, ":", exc)
        return 1
    print("ALL CACHE PERSIST TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
