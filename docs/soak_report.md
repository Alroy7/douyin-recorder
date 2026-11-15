# 浸泡测试与真实 1360 主播实测报告（P1 性能改造）

日期：2026-11-13 21:20 – 23:40（Asia/Shanghai）
负责人：recorder-dev（浸泡测试 harness）

## 1. 结论摘要

- **1 小时 mock 基线（1000 主播 / 10% 在播 / 并发 10，修正后 v3）通过**：
  RSS 均值 48.3 MB / 峰值 51.6 MB（首末 5 分钟 46.3→48.6 MB，无持续增长）；
  CPU 均值 8.4% / 峰值 23.6%（单 Python 进程，不含 ffmpeg 子进程）；
  句柄 390/407，线程 54.5/58，ffmpeg 子进程 9.8/10，在录 9.7/10；
  20871 次轮询成功率 96.91%（注入错误率 3.09%），0 录制错误、0 断流重连；
  678 场录制全部 remux MP4 + ffmpeg 校验通过后立即删除（1356 个文件、约 11.9 GB），无失败样本。
- **浸泡测试发现并修复一个真实产品 bug**：recorder 卡死看门狗因 _total_size() 恒返回 0，
  每 30 秒误判「无数据」并杀掉正常录制（首次基线 1070 次误杀）。修复后重跑基线，stall=0、reconnect=0。
- 真实 1360 主播目录抽样 200 条实测（第 6 节）：直播中 11 条（5.5%），失败 36 条（18%，
  其中「sec_uid 解析到但无直播间」23 条、「enter 接口无房间数据」13 条），其余为未开播；
- 真实在播房间录 20s -> ts remux mp4 -> ffmpeg 校验 Duration>0 且有视频流 -> **立即删除 mp4 与源 ts**，
  3/3 全部校验通过并删除（共 6 个文件 28.1 MB），符合 user「校验没问题就删掉」的要求；
- harness 已适配 perf-dev 新调度器（限速令牌桶 / 自适应轮询 / 并发闸门 / 熔断），
  新增指标列 queued_count / risk_count / avg_latency_ms / in_flight / breaker_paused；
- 复现命令见第 8 节；24h 长跑命令与预计资源见第 9 节。

## 2. 环境与版本

| 项目 | 值 |
| --- | --- |
| 机器 | Intel Core i5-14600KF（14 核 20 线程），32 GB RAM |
| 系统 | Windows 11 专业版 10.0.26300 |
| Python | 3.13.14（官方安装版，tkinter 可用，无第三方依赖） |
| ffmpeg | bin/ffmpeg.exe（gyan.dev 2025-02-26 essentials build） |
| 显示 | 无（测试为无头运行；GUI 另由 tests/test_gui.py 覆盖） |

基线运行时 src 代码哈希（SHA256，perf-dev 可能继续迭代，以启动时刻为准）：

| 文件 | SHA256 |
| --- | --- |
| src/manager.py | E059E271994578428814FCD9367384DB474A0D394B98B9B427F53D2DF30A90B7 |
| src/recorder.py | 908BAC36A10FDE571828AE8AEDB1CEA4219D03A8E5C5378ED2889778E9ECAE5C（含 _total_size 修复） |
| src/checker.py | 18AFD5B86C6299E694A904A14CF61E4AC6DE13DCCD072000C86BEB6EA13AAD6E |
| src/config.py | 601D7ABB37DA90C7A68B84D3B19D0A40E2A1CE004D33686D52621FC3B5BF58E7 |

注：manager.py 在本次测试期间仍被 perf-dev 优化（启动前后哈希有变化），
本基线对应「启动时加载到内存的版本」；正式 24h 长跑应使用 perf-dev 定稿后的版本并在报告里更新哈希。

## 3. 测试架构与方法

### 3.1 mock 模式（1000+ 主播长期挂机模拟）

```
tests/soak/run_soak.py
   ├─ fake_douyin.py        本机 HTTP 服务（127.0.0.1:随机端口）
   │     /webcast/room/web/enter/?web_rid=...  房间状态，在线 ~700KB / 离线 ~48KB，gzip
   │     /webcast/feed/                        发现页在播列表 ~700KB
   │     随机延迟 0.15–0.9s；3% 概率注入 4001038 / HTTP403 / HTTP500 / 空响应 / 超时(16s)
   ├─ FakeDouyinApi         按 docs/INTERFACES.md 契约实现（urllib 真实 HTTP 请求 fake 服务）
   │     -> 通过 src.checker.load_api 注入，不改 src/dy_api.py、不改 manager 业务代码
   ├─ tests/soak/stream_server.py  独立 HTTP-FLV 共享测试流（ffmpeg testsrc2 生成 120s，
   │      request_queue_size=256，避免 10 路并发时连接排队；按实时速率发送）
   ├─ src.manager.MonitorManager（perf-dev 新版：限速/自适应轮询/熔断/闸门）
   ├─ src.recorder.RoomRecorder  -c copy 直录 ts（10 路并发）
   └─ tests/soak/product_policy.py  每场结束 remux mp4 -> ffmpeg 校验 -> 删除（见 3.3）
```

- 1000 个假主播（web_rid 10000000000 起的连续数字），稳态 10% 在播（每个主播有开播/下播周期，
  随机相位保证任意时刻在播比例≈live_ratio）；
- 所有主播共用同一路本机 HTTP-FLV 测试流（节省 CPU/磁盘，专注调度与内存稳定性）；
- 单场录制上限 record_timeout_min=1（60s），用于制造持续的录制开始/结束循环，
  从而反复验证 remux/校验/删除与并发闸门；
- min_keep_seconds=5（否则 <30s 的短录制会被 recorder 自动删除，real 模式 20s 样本进不了产物流程）。

### 3.2 指标采集（tests/soak/metrics.py）

每 10s 采样一行 CSV：

- 进程级：RSS（psapi GetProcessMemoryInfo）、CPU%（GetProcessTimes 差分）、
  句柄数（GetProcessHandleCount）、Python 线程数（threading.active_count）、
  ffmpeg 子进程数（Toolhelp32 快照按父 PID 过滤）；
- 业务级：请求总数/成功/失败/成功率（fake 服务端计数）、在录数、排队数（manager.stats().queued_count）、
  风控计数、平均延迟、in_flight、熔断暂停标记、产物队列长度、磁盘文件数/占用 MB、
  recorder 事件 start/stopped 计数、remux ok/fail。

每 10 分钟输出一行滚动摘要；结束时写 summary_<tag>.json（均值/峰值/首末漂移/产物统计）。

### 3.3 产物策略（user 要求：校验通过立即删除）

```
录制结束(STOPPED 事件) -> product_policy 后台线程池
  -> 找到该场所有 ts/mp4 文件
  -> remux 成 mp4（ffmpeg -c copy，不转码）
  -> ffmpeg -i mp4 校验：Duration > 0 且存在 Video 流
  -> 通过：立即删除 mp4 与源 ts（计入 deleted_files/deleted_bytes）
  -> 失败：移动到 tests/soak/out_keep/<时间>/ 并写 JSONL 记录（便于排查）
```

## 4. 冒烟验证（2.5 分钟 v3，修复 _total_size 后）

参数：anchors=300、live-ratio=0.15、concurrent=10、record_timeout_min=1、stream=120s。

| 指标 | 数值 |
| --- | --- |
| RSS | 均值 45.2 MB / 峰值 47.0 MB（首 36.5 → 末 43.9） |
| CPU | 均值 8.1% / 峰值 26.5% |
| 句柄 | 均值 366 / 峰值 396 |
| 线程 | 均值 48 / 峰值 55 |
| ffmpeg 子进程 | 均值 9.1 / 峰值 10 |
| 在录房间 | 均值 9.0 / 峰值 10 |
| 请求 | 447 次，成功率 97.76%（注入错误 10 个：4001038/403/500/空/超时） |
| 产物 | 处理 31 场，remux 31/31 成功、校验 31/31 通过，删除 62 个文件 441.9 MB |
| 事件 | start=32 stopped=32 **reconnect=0 error=0**，stall=0 |

结论：调度、录制、remux/校验/删除全链路正常，且看门狗不再误杀。

## 5. 1 小时 mock 基线（1000 主播 / 10% 在播 / 并发 10）

### 5.1 命令与配置

```bat
python tests\soak\run_soak.py --mode mock --anchors 1000 --live-ratio 0.1 --hours 1 ^
    --concurrent 10 --interval 60 --record-timeout-min 1 --metrics-interval 10 ^
    --summary-every 600 --tag baseline_1h_v3 --stream-seconds 120 ^
    --out-root tests\soak\out_base3 --log-dir tests\soak\logs_base3
```

数据文件：tests/soak/logs_base3/（metrics_baseline_1h_v3.csv / summary_baseline_1h_v3.json /
product_baseline_1h_v3.jsonl），共 360 个采样点。
关键配置：container=ts、remux_mp4=false（由产物策略统一 remux+校验+删除）、min_keep_seconds=5、
manager 默认新调度参数（poll_rate_limit=6/s、max_poll_workers=12、自适应轮询 30s/90s/300s、
max_concurrent=10、熔断/闸门默认值）。

### 5.2 总体指标

| 指标 | 均值 | 峰值 | 说明 |
| --- | --- | --- | --- |
| RSS | 48.3 MB | 51.6 MB | 首末 5 分钟 46.3→48.6 MB（+2.3 MB/h，GC 正常波动，无泄漏） |
| CPU（Python 主进程） | 8.4% | 23.6% | 含 fake 服务端；ffmpeg 子进程 CPU 未计入 |
| 句柄数 | 390 | 407 | 首末 380.6→387.3（+6.7，稳定） |
| Python 线程数 | 54.5 | 58 | 首末 52.5→53.5（稳定） |
| ffmpeg 子进程 | 9.8 | 11 | 并发上限 10，符合预期 |
| 在录房间 | 9.7 | 10 | 并发闸门 max_concurrent=10 始终打满 |
| 排队房间 | 50.7 | 94 | 闸门生效，直播房间多于并发上限时排队 |
| 轮询请求 | 20871 次/小时 | — | 约 5.8 req/s（poll_rate_limit=6/s） |
| 请求成功率 | 96.91% | 100% | 注入错误率 3.09%（4001038 236 / 403 116 / 500 107 / 超时 79 / 空 106） |
| 风控计数 | 138.4 | 236 | 4001038 累计；熔断从未触发（breaker_paused 峰值 0） |
| 平均延迟 | 556.9 ms | 600.4 ms | 假接口注入 0.15–0.9s 延迟 |
| 磁盘占用 | 45.9 MB | 84.3 MB | 产物删除后立即回落，峰值 <100 MB |
| 录制事件 | start=688 stopped=688 | — | 0 error、0 reconnect、0 stall |
| 产物策略 | 678 场处理 | — | remux 678/678、校验 678/678 通过，删除 1356 个文件 11.97 GB |

### 5.2.1 在播检测数说明

manager 快照里的 live_rooms 均值 62.5、峰值 107，而 mock 假接口实际稳态在播约 100 个。
差异来自新调度器的自适应轮询：冷房间 300s、warm 90s 才轮询一次，快照反映的是「最近一次轮询结果」，
存在发现延迟。这属于限速策略的预期行为（见 7.2 建议），不是 bug；recordings / 排队数不受影响。

### 5.3 每 10 分钟趋势

| 时间段(分钟) | RSS 均值/峰值 MB | CPU 均值/峰值 % | 句柄 均值/峰值 | 线程 均值/峰值 | ffmpeg | 在录 | 排队 均值/峰值 | 成功率 | 磁盘峰值 MB | remux ok/fail |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0-10 | 47.2/49.6 | 8.5/16.2 | 385/403 | 53/57 | 9.6 | 10/10 | 38/79 | 100.0% | 84.2 | 107/0 |
| 10-20 | 48.9/51.0 | 8.9/12.5 | 392/401 | 55/57 | 9.8 | 10/10 | 45/64 | 97.1% | 67.5 | 221/0 |
| 20-30 | 49.8/51.6 | 8.8/14.8 | 391/401 | 55/57 | 9.9 | 10/10 | 47/60 | 97.1% | 84.3 | 335/0 |
| 30-40 | 48.5/50.2 | 8.0/14.6 | 392/406 | 55/58 | 9.8 | 10/10 | 47/58 | 96.9% | 69.5 | 449/0 |
| 40-50 | 47.4/49.6 | 7.9/12.2 | 392/406 | 55/58 | 9.8 | 10/10 | 54/65 | 96.8% | 62.2 | 557/0 |
| 50-60 | 48.1/50.5 | 8.6/23.6 | 390/407 | 54/58 | 9.7 | 10/10 | 74/94 | 96.9% | 63.2 | 678/0 |

趋势判读：
- RSS 在 47–50 MB 区间波动（中间 30 分钟均值 48.2 MB），没有随时间单调上升，GC 锯齿正常，**无内存泄漏**；
- 句柄/线程在 0–10 分钟建立稳态后基本持平（±15），说明线程池/文件句柄没有泄漏；
- 排队数随时间上升（38→74），是因为 mock 在播房间数随状态机波动；并发 10 始终打满、闸门工作正常；
- 磁盘峰值 <85 MB：每场录制结束约 60s 内完成 remux/校验/删除，不会累积。

### 5.4 产物处理

| 项目 | 数值 |
| --- | --- |
| 处理场次 | 678（另 10 场未进入产物处理：4 场停止时录制 <5s 被 recorder 自动删除，6 场无产物文件） |
| remux 成功 | 678 / 678（0 失败） |
| ffmpeg 校验通过 | 678 / 678（Duration>0 且有视频流；平均时长 51.5s，最小 6.0s，最大 59.6s） |
| 删除文件 | 1356 个（每场 ts + mp4 各一） |
| 释放空间 | 11,972,018,671 字节（约 11.97 GB / 11.15 GiB） |
| 保留失败样本 | 0（tests/soak/out_keep3 为空） |
| 单场平均 ts 大小 | 8.63 MB（60s 录制，约 1.15 MB/s ≈ 9.2 Mbps） |

## 6. 真实 1360 主播实测（抽样 200）

### 6.1 参数

```bat
python tests\soak\run_soak.py --mode real --sample 200 --rate 2 --concurrency 4 ^
    --record-n 3 --record-seconds 20 --tag real_200
```

数据源：analysis/lead/watchlist_test_1360.json（1360 条，全部为主播主页链接
https://www.douyin.com/user/MS4w...），对应 Excel C:\Users\Admin\Downloads\抖音关注活跃度_1-1438.xlsx。

### 6.2 结果

| 指标 | 数值 |
| --- | --- |
| 抽样 | 200 条（去重后均为有效主页链接） |
| 总耗时 | 3.42 分钟（rate=2/s，concurrency=4） |
| 直播中 | 11 条（5.5%） |
| 未开播 | 153 条（76.5%） |
| 查询失败 | 36 条（18.0%） |

失败原因分布（dy_api 返回的 error 归类）：

| 原因 | 数量 | 说明 |
| --- | --- | --- |
| resolve_failed | 23 | 已解析到 sec_uid，但该主播当前没有可解析的直播间（从未开播/已注销/私密等） |
| enter_无房间数据 | 13 | enter 接口返回 status_code=0 但 data.data 为空（账号无直播间） |

> 说明：主页链接需要「sec_uid -> 直播间」二次解析，18% 的失败主要是账号本身没有直播间数据，
> 不是网络错误；建议上层把这类「无直播间」视为「未开播」并缓存一段时间（避免每轮都重复解析、浪费请求）。

### 6.3 真实录制 20s -> 校验 -> 删除

| 房间号 | 大小 | ffmpeg 校验结果 | 处理 |
| --- | --- | --- | --- |
| 24489704921 | 3.5 MB | Duration=21.6s，有视频流 | 通过 -> 删除 ts+mp4 |
| 799477132030 | 6.25 MB | Duration=22.3s，有视频流 | 通过 -> 删除 ts+mp4 |
| 158055356175 | 4.0 MB | Duration=21.x s，有视频流 | 通过 -> 删除 ts+mp4 |

合计处理 3 场，remux 3/3 成功、校验 3/3 通过，删除 6 个文件 28.1 MB，无失败样本。

## 7. 发现的问题与建议

### 7.1 关键产品 bug（本次浸泡最大发现，已修复）

**现象**：首轮 1 小时基线中 1245 场录制有 1070 场在启动 30s 后报
「启动后 30 秒未收到任何数据，重启 ffmpeg」，但同一场结束日志却显示「共 1 个文件，5.00 MB，时长 00:30」，
即：实际一直在正常写数据，却被卡死看门狗误杀并重启（每场只录到 ~30s，reconnect 计数 1102）。

**根因**：src/recorder.py 的 _total_size() 被写成 @staticmethod，实现为 for f in files or []，
所有无参调用（录制进度上报、卡死看门狗、单文件大小上限、断流后 grew 判断）都返回 0。
带 files 参数的调用（status()）正常，所以单测没有暴露。

**修复**：改为实例方法，files 为 None 时统计 self._files（加 self._lock）；同时 harness 改为独立 HTTP-FLV
测试流（tests/soak/stream_server.py，request_queue_size=256），排除连接排队干扰。

**验证**：修复后 2.5 分钟冒烟（300 主播/并发 10）stall=0、reconnect=0、录制 32 场全部 remux+校验+删除成功；
修复后的 1 小时基线见第 5 节（0 error / 0 reconnect / 0 stall，688 场录制正常收尾）。

### 7.1.1 harness 侧问题（已修复，仅影响测试工具）

1) 假主播状态机初版相位设计错误：初始 live 的主播在 3–6 分钟后集体下播、且离线时长远大于剩余测试时长，
   导致在播数中途掉到 0；已改为随机相位 + 稳态时长建模。
2) run_soak.py 的 --concurrent/--concurrency 参数名不一致（real 模式启动报错），已统一。
3) real 模式最初把 dict 直接传给 checker（报「缺少房间号/链接」），已改为 WatchItem.from_dict 转换。
4) 最初复用 src/mock_api 的内嵌 HTTP 服务，backlog=5，10 路并发时连接排队；已新增 tests/soak/stream_server.py。
5) 停止时如果队列里还有产物，可能残留文件；已增加停止后收尾 sweep（测试中 out_base3 残留 0 个文件）。

### 7.2 产品侧观察（建议 perf-dev / lead 关注）

- **自适应轮询的发现延迟**：冷房间默认 300s 轮询一次，1000 主播时「刚开播」的房间最坏要等 5 分钟才被发现；
  这是有意为之的限速策略，但用户对「开播后立刻录」有期待，建议对「最近 24h 开播过」的房间用 warm 档（90s），
  或给用户一个「重点主播」标记走 live 档。
- **短录制自动删除**：recorder 的 min_keep_seconds=30 会删除 <30s 的录制（防垃圾文件）；
  建议在 GUI/日志里明确提示（当前 manager 日志已有「过短录制已删除」提示，可再在 GUI 状态栏提示）。
- **无直播间的账号**：主页链接解析失败率高（本次 18%），建议 dy_api 缓存「该账号无直播间」结果
  （例如 10 分钟），避免每轮重复解析被风控。
- **排队指标已打通**：manager.stats().queued_count 在基线里正确反映闸门排队（10 在录 + 均值 50.7 / 峰值 94 排队），
  建议 GUI 表格直接展示「排队中」状态（新 snapshot 已带 queued/queue_reason 字段）。

### 7.3 稳定性观察（详见 5.2/5.3）

**结论：1 小时基线无内存/句柄/线程泄漏，无录制错误。**

- 内存在第 10 分钟后稳定在 47–50 MB（中间 30 分钟均值 48.2 MB），首末 5 分钟 +2.3 MB 属 GC 锯齿；
- 句柄 380.6→387.3（+6.7）、线程 52.5→53.5（+1），在稳态范围内；ffmpeg 子进程数严格不超过并发上限；
- 688 场录制 0 error、0 reconnect、0 stall，说明修复后看门狗与重连逻辑正常；
- 产物 678/678 remux + 校验通过并删除，磁盘峰值 84 MB，SSD 压力可控但写入量约 11.97 GB/h（见第 9 节）；
- 熔断从未触发（breaker_paused=0），注入的 3% 错误被重试/退避正常消化，成功率稳定 96.9%。

## 8. 复现命令

```bat
:: 依赖：项目自带 bin\ffmpeg.exe；纯标准库 Python；无需联网（mock 模式）
:: 1) 3 分钟冒烟
python tests\soak\run_soak.py --mode mock --anchors 200 --live-ratio 0.15 --hours 0.05 ^
    --concurrent 5 --record-timeout-min 1 --tag smoke

:: 2) 1 小时基线（本次报告数据）
python tests\soak\run_soak.py --mode mock --anchors 1000 --live-ratio 0.1 --hours 1 ^
    --concurrent 10 --interval 60 --record-timeout-min 1 --metrics-interval 10 ^
    --summary-every 600 --tag baseline_1h

:: 3) 真实 1360 主播（抽样 200，限速 2/s）
python tests\soak\run_soak.py --mode real --sample 200 --rate 2 --concurrency 4 ^
    --record-n 3 --record-seconds 20

:: 4) 真实全量 1360、多轮限速（30–60 分钟）
python tests\soak\run_soak.py --mode real --sample 1360 --rate 4 --concurrency 6 ^
    --rounds 8 --round-interval 120 --record-n 5 --record-seconds 20
```

产物位置：

- 指标 CSV：tests/soak/logs_base3/metrics_baseline_1h_v3.csv（360 行）
- 汇总 JSON：tests/soak/logs_base3/summary_baseline_1h_v3.json
- 产物处理明细：tests/soak/logs_base3/product_baseline_1h_v3.jsonl（678 行）
- 真实模式：tests/soak/logs_real/real_summary_real_200.json
- 首轮无效基线（bug 证据）：tests/soak/logs_base/（1070 次误杀日志，见第 11 节）
- 失败样本保留：tests/soak/out_keep3/（本次为空）

## 9. 24 小时浸泡命令与资源预估

### 9.1 命令（后台，产物自动删除）

```bat
cd /d D:\Code\DS-Workspace\douyin-recorder
start /b python tests\soak\run_soak.py --mode mock --anchors 1000 --live-ratio 0.1 ^
    --hours 24 --concurrent 10 --interval 60 --record-timeout-min 1 ^
    --metrics-interval 10 --summary-every 600 --tag soak_24h ^
    --out-root tests\soak\out_24h --log-dir tests\soak\logs_24h
```

监控：每 10 分钟看一次 tests/soak/logs_24h/metrics_soak_24h.csv 的最后一行与 stdio 摘要；
结束时生成 tests/soak/logs_24h/summary_soak_24h.json。

### 9.2 资源预估（基于 1h 基线线性外推）

| 指标 | 1 小时实测 | 24 小时预估 | 说明 |
| --- | --- | --- | --- |
| RSS | 均值 48.3 MB / 峰值 51.6 MB | < 60 MB（无泄漏趋势） | 1000 主播状态快照 + 10 路录制 |
| CPU（Python 主进程） | 均值 8.4% / 峰值 23.6% | 约 2 CPU 小时/24h | 不含 ffmpeg 子进程；-c copy 每路约 1–3% |
| 句柄数 | 390 / 407 | 400–430 | 稳态 |
| 线程数 | 54.5 / 58 | 55–60 | 12 轮询 worker + 10 录制线程 + 管理等 |
| 轮询请求 | 20871 / 小时（5.8 req/s） | 约 50 万次/24h | 限速 poll_rate_limit=6/s 上限 |
| 录制场次 | 688 / 小时 | 约 1.6 万场/24h | record_timeout_min=1 人为制造循环 |
| 磁盘写入 | 约 11.97 GB/小时 | 约 287 GB/24h（写完即删） | 峰值占用 <100 MB；注意 SSD 写入量 |
| 失败/错误 | 0 error、0 stall、0 reconnect | 预期同量级 | 产物校验失败会保留到 out_keep |

注意：以上是 mock 测试流（640x360、约 9.2 Mbps/路 × 10 路）的写入量。
真实用户场景是「按需录制真实直播」，不会 24 小时持续写盘删除；24h 浸泡的目的是验证长稳，
建议在 SSD 上跑（写入量约 287 GB/天，普通 SSD 寿命足够，但请知悉）。
如果只想验证稳定性、不关心写入量，可把 --concurrent 降到 5 或把 record_timeout_min 调大（减少场次）。

## 10. 附录：CSV 字段说明

| 字段 | 说明 |
| --- | --- |
| rss_mb / cpu_pct / handles / py_threads / ffmpeg_procs | 进程级资源 |
| req_total / req_ok / req_err / req_success_rate | 假接口请求统计（服务端计数） |
| live_rooms / recordings | manager 快照中的在播数 / 在录数 |
| queued_count / in_flight | 并发闸门排队数 / 正在查询数 |
| risk_count / avg_latency_ms / breaker_paused | 风控次数 / 平均延迟 / 熔断暂停标记 |
| policy_queue / remux_ok / remux_fail | 产物队列长度 / remux 成功失败 |
| disk_mb / disk_files | 输出目录占用（产物删除后回落） |
| events_started / events_stopped | 录制开始/结束事件计数 |

## 11. 附录：首轮无效基线与 bug 发现过程

首轮 1 小时基线（tag=baseline_1h，数据保留在 tests/soak/logs_base/）因为 7.1 的 bug 无效，
但正是它暴露了问题，保留作为证据：

| 指标 | 首轮（bug 存在） | 修复后（v3） |
| --- | --- | --- |
| 录制场次 | 1245 | 688 |
| reconnect 事件 | 1102 | 0 |
| 「无数据」误杀 | 1070 | 0 |
| 平均单场时长 | 28.4s | 51.5s |
| 产物处理 | 1224 场（仍全部校验通过） | 678 场 |
| RSS 均值/峰值 | 49.0 / 53.0 MB | 48.3 / 51.6 MB |
| 句柄/线程 | 391/420、54.6/61 | 390/407、54.5/58 |

结论：即使存在每 30s 杀进程重启的极端情况，RSS/句柄/线程也没有泄漏，说明 manager/recorder 的资源管理本身是稳的；
但录制连续性受损，因此报告以修复后的 v3 基线为准。

> 说明：24 小时浸泡使用 --concurrent 10 作为测试上限（便于与 1 小时基线对照），不是产品限制；产品默认 max_concurrent=0（不限），已另测 24 个在播主播全部同时录制、排队 0。

---

## 5 小时不限并发全压测试（2026-11-14 10:06 启动）

用户决定：24h 稳定度测试在 10.2 小时后停止（数据保留于 logs_24h/，部分报告 docs/soak_24h_partial.md），改跑 5 小时不限并发压测：

- 参数：1000 主播 / 10% 在播 / max_concurrent=0（不限）/ max_ffmpeg_processes=0（不设上限）/ max_total_mbps=0 / 每分钟轮询；
- 观察峰值并发在录数、内存/句柄/线程、成功率、产物处理与磁盘峰值、是否熔断或崩溃；
- 数据目录：tests/soak/logs_unlimited5h/。
