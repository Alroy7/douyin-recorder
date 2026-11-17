# -*- coding: utf-8 -*-
"""系统容量探测与自动调优测试。

覆盖：
  1. _parse_bps 各种网卡速度字符串解析；
  2. recommend() 纯计算：带宽探测到/未探测到、并发推荐、min_free_gb 病态下调；
  3. apply_recommendations()：只回填 0（自动）项、不动 max_concurrent、病态下调落盘。

运行：python tests/test_capacity.py
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.config import Config
from src import capacity

TMP = ROOT / "tests" / "tmp" / "capacity"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)


def test_parse_bps():
    assert capacity._parse_bps("1000000000") == 1000     # bps -> 1000 Mbps
    assert capacity._parse_bps("1 Gbps") == 1000
    assert capacity._parse_bps("100 Mbps") == 100
    assert capacity._parse_bps("2500000000") == 2500
    assert capacity._parse_bps("") is None
    assert capacity._parse_bps("0") is None
    assert capacity._parse_bps("abc") is None
    print("PASS _parse_bps 解析")


def _cfg(**overrides):
    path = TMP / "cfg.json"
    data = {
        "output_dir": str(TMP / "out"),
        "min_free_gb": 2.0,
        "max_concurrent": 0,
        "max_total_mbps": 0,
        "max_ffmpeg_processes": 0,
        "max_poll_workers": 0,
        "remux_concurrency": 0,
        "auto_tune_limits": True,
    }
    data.update(overrides)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return Config.load(path)


def test_recommend_with_network():
    cfg = _cfg()
    rec = capacity.recommend(cfg, free_gb=100.0, network_mbps=1000, cores=16)
    assert rec["max_total_mbps"] == 750, rec
    assert rec["max_ffmpeg_processes"] == 128, rec          # 16*8
    assert rec["max_poll_workers"] == 32, rec               # 16*2
    assert rec["remux_concurrency"] == 8, rec               # 16//2
    assert rec["min_free_gb"] is None, rec                  # 2.0 < 100*0.5，不动
    assert any("1000 Mbps" in n for n in rec["notes"]), rec["notes"]
    print("PASS recommend 有网卡带宽")


def test_recommend_without_network():
    cfg = _cfg()
    rec = capacity.recommend(cfg, free_gb=100.0, network_mbps=None, cores=8)
    assert rec["max_total_mbps"] is None, rec               # 未探测 -> 保持不限
    assert any("不限" in n for n in rec["notes"]), rec["notes"]
    print("PASS recommend 无网卡带宽保持不限")


def test_recommend_pathological_disk():
    cfg = _cfg(min_free_gb=25.0)
    rec = capacity.recommend(cfg, free_gb=25.0, network_mbps=None, cores=8)
    assert rec["min_free_gb"] is not None, rec
    assert 2.0 <= rec["min_free_gb"] <= 10.0, rec
    assert rec["min_free_gb"] < 25.0, rec
    assert any("过于激进" in n for n in rec["notes"]), rec["notes"]
    print("PASS recommend 病态 min_free_gb 下调 ->", rec["min_free_gb"])


def test_recommend_normal_disk_untouched():
    cfg = _cfg(min_free_gb=2.0)
    rec = capacity.recommend(cfg, free_gb=100.0, network_mbps=None, cores=8)
    assert rec["min_free_gb"] is None, rec
    print("PASS recommend 正常 min_free_gb 不动")


def test_apply_recommendations_fill_only_zero():
    cfg = _cfg(min_free_gb=2.0)
    orig_net = capacity.network_downlink_mbps
    orig_free = capacity._disk_free_gb
    capacity.network_downlink_mbps = lambda: 1000
    capacity._disk_free_gb = lambda *_a, **_k: 100.0
    try:
        res = capacity.apply_recommendations(cfg)
    finally:
        capacity.network_downlink_mbps = orig_net
        capacity._disk_free_gb = orig_free
    applied = res["applied"]
    assert applied.get("max_total_mbps") == 750, applied
    assert applied.get("max_ffmpeg_processes") >= 64, applied
    # max_poll_workers 的合法范围是 1~128（无法为 0），默认 12 已够用，不参与自动回填
    assert cfg["max_concurrent"] == 0, "max_concurrent 绝不能被改"
    assert cfg["min_free_gb"] == 2.0, "正常 min_free_gb 不应被改"
    # 已回填后再次 apply 不应再改（幂等）
    capacity.network_downlink_mbps = orig_net
    capacity._disk_free_gb = orig_free
    res2 = capacity.apply_recommendations(cfg)
    assert "max_total_mbps" not in res2["applied"], res2
    print("PASS apply 只回填 0 项、不动 max_concurrent、幂等")


def test_apply_recommendations_fixes_pathological_disk():
    cfg = _cfg(min_free_gb=25.0)
    orig_net = capacity.network_downlink_mbps
    orig_free = capacity._disk_free_gb
    capacity.network_downlink_mbps = lambda: None
    capacity._disk_free_gb = lambda *_a, **_k: 25.0
    try:
        res = capacity.apply_recommendations(cfg)
    finally:
        capacity.network_downlink_mbps = orig_net
        capacity._disk_free_gb = orig_free
    assert res["applied"].get("min_free_gb") is not None, res
    assert cfg["min_free_gb"] < 25.0, cfg["min_free_gb"]
    print("PASS apply 病态 min_free_gb 落盘下调 ->", cfg["min_free_gb"])


def main():
    tests = [
        ("_parse_bps 解析", test_parse_bps),
        ("recommend 有带宽", test_recommend_with_network),
        ("recommend 无带宽", test_recommend_without_network),
        ("recommend 病态磁盘", test_recommend_pathological_disk),
        ("recommend 正常磁盘", test_recommend_normal_disk_untouched),
        ("apply 只回填 0 项", test_apply_recommendations_fill_only_zero),
        ("apply 病态磁盘落盘", test_apply_recommendations_fixes_pathological_disk),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failed.append((name, str(exc)))
    print()
    if failed:
        print("FAILED:")
        for n, e in failed:
            print("  -", n, ":", e)
        return 1
    print("ALL CAPACITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
