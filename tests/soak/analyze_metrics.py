# -*- coding: utf-8 -*-
"""分析浸泡测试 CSV/JSON，输出 Markdown 表格（写报告用）。

用法：
    python tests/soak/analyze_metrics.py tests/soak/logs_base/metrics_baseline_1h.csv
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def load_csv(path: Path):
    with open(path, "r", encoding="utf-8", newline="") as fp:
        return list(csv.DictReader(fp))


def fnum(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def bucket_rows(rows, bucket_seconds=600.0):
    buckets = []
    current = []
    start = fnum(rows[0].get("elapsed_s")) if rows else 0.0
    for row in rows:
        elapsed = fnum(row.get("elapsed_s"))
        if elapsed - start >= bucket_seconds:
            buckets.append((start, current))
            current = []
            start = elapsed
        current.append(row)
    if current:
        buckets.append((start, current))
    return buckets


def agg(rows, key):
    vals = [fnum(r.get(key)) for r in rows]
    if not vals:
        return 0.0, 0.0
    return sum(vals) / len(vals), max(vals)


def markdown_table(rows, bucket_seconds=600.0):
    buckets = bucket_rows(rows, bucket_seconds)
    lines = []
    lines.append("| 时间段(分钟) | RSS 均值/峰值 MB | CPU 均值/峰值 % | 句柄 均值/峰值 | 线程 均值/峰值 | ffmpeg | 在录 | 排队 | 成功率 | 磁盘峰值 MB | remux ok/fail |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for idx, (start, bucket) in enumerate(buckets, 1):
        rss_m, rss_x = agg(bucket, "rss_mb")
        cpu_m, cpu_x = agg(bucket, "cpu_pct")
        h_m, h_x = agg(bucket, "handles")
        t_m, t_x = agg(bucket, "py_threads")
        ff_m, _ = agg(bucket, "ffmpeg_procs")
        rec_m, rec_x = agg(bucket, "recordings")
        q_m, q_x = agg(bucket, "queued_count")
        _, sr = agg(bucket, "req_success_rate")
        _, disk_x = agg(bucket, "disk_mb")
        remux_ok = int(fnum(bucket[-1].get("remux_ok")))
        remux_fail = int(fnum(bucket[-1].get("remux_fail")))
        lines.append("| %d-%d | %.1f/%.1f | %.1f/%.1f | %.0f/%.0f | %.0f/%.0f | %.1f | %.0f/%.0f | %.0f/%.0f | %.1f%% | %.1f | %d/%d |" % (
            idx * bucket_seconds / 60.0 - bucket_seconds / 60.0, idx * bucket_seconds / 60.0,
            rss_m, rss_x, cpu_m, cpu_x, h_m, h_x, t_m, t_x, ff_m,
            rec_m, rec_x, q_m, q_x, sr, disk_x, remux_ok, remux_fail))
    return "\n".join(lines)


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else Path("tests/soak/logs_base/metrics_baseline_1h.csv")
    rows = load_csv(path)
    if not rows:
        print("no rows")
        return 1
    print("samples:", len(rows), "duration_min: %.1f" % (fnum(rows[-1].get("elapsed_s")) / 60.0))
    for key in ("rss_mb", "cpu_pct", "handles", "py_threads", "ffmpeg_procs",
                "recordings", "live_rooms", "queued_count", "req_success_rate", "disk_mb"):
        mean, max_v = agg(rows, key)
        print("%-18s mean=%10.2f max=%10.2f" % (key, mean, max_v))
    print()
    print(markdown_table(rows))
    print()
    # 首末 5 分钟对比（内存漂移）
    head = rows[:30]
    tail = rows[-30:]
    print("head5min rss=%.2f handles=%.1f threads=%.1f" % (
        agg(head, "rss_mb")[0], agg(head, "handles")[0], agg(head, "py_threads")[0]))
    print("tail5min rss=%.2f handles=%.1f threads=%.1f" % (
        agg(tail, "rss_mb")[0], agg(tail, "handles")[0], agg(tail, "py_threads")[0]))
    summary_path = path.with_name("summary_" + path.stem.replace("metrics_", "") + ".json")
    if summary_path.exists():
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        print("summary:", json.dumps({
            "metrics": data.get("metrics"),
            "policy": data.get("policy"),
            "events": data.get("event_counts"),
            "fake": data.get("fake_stats"),
        }, ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
