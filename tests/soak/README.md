# tests/soak —— 浸泡测试 harness

用于验证「1000+ 房间长期挂机」的稳定性：内存/句柄/线程/CPU 曲线、轮询成功率、在录与排队、
产物「remux mp4 -> ffmpeg 校验可读 -> 删除」全链路。

## 文件

| 文件 | 说明 |
| --- | --- |
| fake_douyin.py | 本机假抖音接口：enter/feed 接口、真实响应体（在线 ~700KB/离线 ~48KB）、gzip、0.15–0.9s 随机延迟、按概率注入 4001038/403/500/空响应/超时 |
| run_soak.py | 浸泡入口：mock 模式（1000 假主播 + 共享 HTTP-FLV 测试流）/ real 模式（1360 主播主页链接实测） |
| metrics.py | Windows ctypes 采样：RSS/CPU/句柄/Python 线程/ffmpeg 子进程 + 业务指标，写 CSV + 每 10 分钟摘要 |
| product_policy.py | 产物策略：每场结束 remux mp4 -> ffmpeg 校验（Duration>0 有视频流）-> 校验通过立即删除 mp4 与源 ts；失败保留到 out_keep |
| stream_server.py | 独立 HTTP-FLV 测试流服务（request_queue_size=256，支持 10+ 并发客户端，按实时速率发送） |

## 快速开始

### mock 基线（1000 主播 / 10% 在播 / 并发 10 / 1 小时）

```bat
python tests\soak\run_soak.py --mode mock --anchors 1000 --live-ratio 0.1 ^
    --hours 1 --concurrent 10 --interval 60 --record-timeout-min 1 ^
    --metrics-interval 10 --summary-every 600 --tag baseline_1h
```

### mock 24 小时（后台，产物自动删）

```bat
start /b python tests\soak\run_soak.py --mode mock --anchors 1000 --live-ratio 0.1 ^
    --hours 24 --concurrent 10 --interval 60 --record-timeout-min 1 ^
    --metrics-interval 10 --summary-every 600 --tag soak_24h ^
    --out-root tests\soak\out_24h --log-dir tests\soak\logs_24h
```

### 真实 1360 主播（限速轮询）

```bat
python tests\soak\run_soak.py --mode real --sample 200 --rate 2 --concurrency 4 ^
    --record-n 3 --record-seconds 20
```

全量 1360、多轮（30–60 分钟）：

```bat
python tests\soak\run_soak.py --mode real --sample 1360 --rate 4 --concurrency 6 ^
    --rounds 8 --round-interval 120 --record-n 5 --record-seconds 20
```

## 输出

- tests/soak/logs/metrics_<tag>.csv：每 10s 一行，字段见 metrics.py 的 FIELDS；
- tests/soak/logs/summary_<tag>.json：结束汇总（均值/峰值/漂移/请求/产物统计）；
- tests/soak/logs/product_<tag>.jsonl：每场产物的 remux/校验/删除记录；
- tests/soak/out_keep/：校验失败被保留的样本（正常情况下为空）；
- tests/soak/out/：录制中的 ts（处理完即删）。

## 适配 perf-dev 新调度器

manager 的新配置（poll_rate_limit / poll_interval_live|warm|cold / max_concurrent=0 不限 /
max_total_mbps / max_ffmpeg_processes 等）可以直接用 --config-set key=value 覆盖，例如：

```bat
python tests\soak\run_soak.py --mode mock --anchors 1000 --hours 1 ^
    --config-set poll_rate_limit=8 --config-set max_poll_workers=16 ^
    --config-set poll_interval_live=30 --config-set max_total_mbps=200
```

--config-set 的值按 JSON 解析（int/float/bool/数组），解析失败按字符串处理。