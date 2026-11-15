# 24 小时浸泡测试收尾清单（断点续做）

> 当前状态（2026-11-14 09:45）：24h 浸泡运行中（PID 25456，23:37 启动），已跑 10.1 小时；
> 交付版本 v2.3.2 已打包；12 套测试全绿；只差 24h 数据落报告与最终验收。

## 1. 测试进程信息

| 项 | 值 |
|---|---|
| 进程 | PID 25456（python tests/soak/run_soak.py ...） |
| 启动 / 预计结束 | 2026-11-13 23:37 / 2026-11-14 23:37 |
| 指标 CSV | tests/soak/logs_24h/metrics_soak_24h.csv（10s 采样） |
| 汇总 JSON | tests/soak/logs_24h/summary_soak_24h.json（结束时生成） |
| 产物明细 | tests/soak/logs_24h/product_soak_24h.jsonl |
| 日志 | tests/soak/logs_24h/soak.log、stdout.log |
| 测试参数 | mock 1000 主播 / 10% 在线 / 并发上限 10（测试参数，不是产品限制） |

## 2. 判定标准（测试结束时）

- 进程正常退出（或已生成 summary JSON）；
- RSS 峰值 < 150MB（当前 10.1h 约 64MB、斜率 ~1MB/h，预计 24h 约 78–85MB，其中约 8.6MB 是测试脚本指标行缓存）；
- 句柄 < 600、Python 线程 < 100；成功率 > 95%；0 卡死/0 熔断误触发；
- 产物 remux 校验全部通过、删除数/字节数正常（磁盘峰值 < 200MB）。

## 3. 收尾步骤（测试结束后一条条做）

1) 确认进程已结束、summary JSON 已生成：
   python analysis/lead/report_24h.py --log-dir tests/soak/logs_24h --tag soak_24h
   → 打印 24h 指标表；如数据正常，追加到报告：
   python analysis/lead/report_24h.py --log-dir tests/soak/logs_24h --tag soak_24h --append
2) 用趋势脚本核对 RSS/句柄/线程/成功率（analysis/lead/trend_soak.py）；
3) 填 docs/acceptance_v21.md 第 2 节「24h 浸泡」行（把"运行中"改为实测数据）与第 6 节状态；
4) README「更新日志」补最终版本条目（若版本未变则补 24h 结论）；
5) 确认最终包：dist/DouyinRecorder_win64.zip（v2.3.2，45.9MB）+ --version/--check；
6) 清理测试残留：tests/soak/out_24h 应只剩空目录/少量文件（产物已在测试中即删）；
7) 向用户交付总结：改造点、性能数据（1h 基线 + 24h）、真实 1360 列表验证、验收清单、包与启动方式。

## 4. 已完成证据（无需重做）

- 1h 基线（修正后 v3）：RSS 48.3MB、CPU 8.4%、20871 次轮询/小时、成功率 96.91%、678 场录制全部 remux+校验+删除、0 卡死/0 熔断；
- 24h 前 10 小时：RSS 平台期 60–65MB、句柄/线程稳定、成功率 97.0%、在录 10/排队 ~75、磁盘即删；
- 真实 1360 列表：300 条冷启动实测（在播 5.7%、HTTP 0 错误、gzip 5.8x、4 条连接）；同进程两轮 10x 提速；缓存落盘重启 12.5x；
- GUI 1360 条规模：刷新 10–13ms、搜索 11ms；设置页/记录页/日志页回归通过；
- 打包版 Excel 导入实测 1360 条、去重生效；
- 单实例锁、断流降档、看门狗修复均有测试；
- 12 套测试全绿（recorder/manager/gui/min_keep/ffmpeg_detect/history/scheduler/import/quality_degrade/cache_persist/instance_guard/real）。

## 5. 失败注入说明

浸泡测试 mock 层注入了 3% 错误（http403/4001038/http500/timeout/empty），因此成功率 ~97% 属预期；
0 断流/0 卡死/0 熔断误触发；产物处理 100% 通过。
