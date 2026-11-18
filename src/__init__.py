# -*- coding: utf-8 -*-
"""抖音直播录制工具 —— 本地自用版。

模块划分：
    config.py       配置加载/默认值/校验
    model.py        数据模型（主播条目、运行时状态、事件、文件名模板）
    storage.py      watchlist.json 读写（原子写、损坏备份）
    logger.py       日志初始化（文件 UTF-8 + 控制台）
    ffmpeg_util.py  ffmpeg 探测、remux、子进程清理
    disk.py         磁盘空间检查与保留策略清理
    recorder.py     单房间录制引擎（ffmpeg 子进程/断流重连/分段）
    checker.py      直播状态查询（封装 src/dy_api.py，失败降级 mock）
    manager.py      监控调度（轮询/自动录制/事件分发/状态快照）
    mock_api.py     离线 mock，供无 dy_api 时自测
    gui.py          tkinter 图形界面（风格 A「青屿」）
    ui_theme.py     UI 主题与自定义控件
    history.py      运行记录（SQLite：录制/运行/错误）
    cli.py          命令行模式
"""

__version__ = "2.5.7"
