#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音直播录制工具（本地自用版）入口。

用法：
    python run.py                     启动图形界面（默认）
    python run.py --cli               无 GUI 常驻监控（Ctrl+C 退出）
    python run.py --once              只查一次直播状态，不录制
    python run.py --check             环境自检（python/ffmpeg/目录/协议层/日志）
    python run.py --list              列出主播列表
    python run.py --add <链接/房间号> [--name 主播名]   添加主播
    python run.py --remove <id>       删除主播
    python run.py --history [--days N]               查看最近录制记录与统计
    python run.py --export-diagnostics <zip路径> [--days N]  导出脱敏诊断包

可选参数：
    --config PATH       指定 config.json（默认项目根 config.json）
    --watchlist PATH    指定 watchlist.json
    --log-level LEVEL   DEBUG/INFO/WARNING/ERROR
    --mock              本次运行强制使用内置 mock（离线演示/测试）
    --days N            与 --history/--export-diagnostics 配合：统计/日志天数
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# dy_api 可能依赖 .pylibs 里解包的第三方库（requests 等）
_PYLIBS = ROOT / ".pylibs"
if _PYLIBS.is_dir() and str(_PYLIBS) not in sys.path:
    sys.path.insert(0, str(_PYLIBS))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="抖音直播录制工具（本地自用版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cli", action="store_true", help="无 GUI 常驻监控")
    parser.add_argument("--once", action="store_true", help="只查询一次状态")
    parser.add_argument("--check", action="store_true", help="环境自检")
    parser.add_argument("--list", action="store_true", help="列出主播列表")
    parser.add_argument("--add", metavar="TARGET", help="添加主播（链接/房间号/分享文案）")
    parser.add_argument("--name", default="", help="与 --add 配合，指定主播名")
    parser.add_argument("--remove", metavar="ID", help="按 id 或房间号删除主播")
    parser.add_argument("--history", action="store_true", help="查看最近录制记录与统计")
    parser.add_argument("--days", type=int, default=0,
                        help="与 --history/--export-diagnostics 配合：统计/日志天数")
    parser.add_argument("--export-diagnostics", metavar="ZIP", default="",
                        help="导出脱敏诊断包（zip 路径）")
    parser.add_argument("--config", default="", help="config.json 路径")
    parser.add_argument("--watchlist", default="", help="watchlist.json 路径")
    parser.add_argument("--log-level", default="", help="日志级别")
    parser.add_argument("--mock", action="store_true", help="强制使用内置 mock")
    parser.add_argument("--activity-report", metavar="PATH", default="",
                        help="导出主播活跃度 CSV（最近开播时间/未开播天数），默认 activity_report.csv")
    parser.add_argument("--convert-leftovers", action="store_true",
                        help="把输出目录中未转 MP4 的 .flv/.ts 补转为 MP4 后退出")
    parser.add_argument("--cleanup", action="store_true",
                        help="清理输出目录：补转残留 flv/ts，并删除时长低于 min_keep_seconds 的短视频")
    parser.add_argument("--export-watchlist", metavar="PATH", default="",
                        help="把当前主播列表（含分组/画质/开关等）导出为 xlsx/csv")
    parser.add_argument("--import-file", metavar="PATH", default="",
                        help="从 Excel/CSV 批量导入主播后退出（自动识别表头与去重）")
    parser.add_argument("--import-group", default="", help="批量导入的主播分组名")
    parser.add_argument("--import-quality", default="", help="批量导入的默认画质（origin/best/hd/sd）")
    parser.add_argument("--version", action="store_true", help="显示版本")
    return parser


def env_check(cfg, watchlist) -> int:
    """环境自检，输出诊断信息。"""
    import platform
    from src import ffmpeg_util
    from src import history
    from src.checker import load_api
    from src import disk as diskmod

    print("===== 环境自检 =====")
    print("Python      : %s (%s)" % (sys.version.split()[0], sys.executable))
    print("操作系统    : %s" % platform.platform())
    print("项目根目录  : %s" % ROOT)
    print("配置文件    : %s" % cfg.path)
    print("主播列表    : %s（%d 个主播）" % (watchlist.path, len(watchlist)))
    print("输出目录    : %s（磁盘剩余 %.1f GB）" % (cfg.output_root, diskmod.free_gb(cfg.output_root)))
    print("日志目录    : %s（保留 %d 天）" % (cfg.log_dir_path, cfg.get_int("log_keep_days")))
    print("历史数据库  : %s" % history.default_db_path())
    try:
        from src import dy_api as _dy_api
        _ci = _dy_api.get_resolve_cache_info()
        print("解析缓存    : %s（有效 %s 条 / 过期 %s 条）" % (
            _ci.get("cache_file"), _ci.get("entries_valid", 0), _ci.get("entries_expired", 0)))
    except Exception as _exc:  # 兼容旧版本 dy_api
        print("解析缓存    : （不可用：%s）" % _exc)
    print("最短保留时长: %s" % (
        ("关闭（0）" if cfg.get_int("min_keep_seconds") <= 0
         else "%d 秒（低于该时长的录制会自动删除）" % cfg.get_int("min_keep_seconds"))))
    configured_ffmpeg = cfg.get_str("ffmpeg_path").strip() or (
        "（自动：项目 bin -> 原版目录 -> PATH -> 常见目录 -> 注册表）")
    print("ffmpeg 配置 : %s" % configured_ffmpeg)
    ok, ffmpeg, info = ffmpeg_util.check_ffmpeg(cfg.get_str("ffmpeg_path"))
    print("ffmpeg 解析 : %s" % (ffmpeg if ok else "未找到"))
    for line in str(info or "").splitlines():
        print("              %s" % line)
    api, name, err = load_api(cfg.get_bool("use_mock_api"))
    print("协议层      : %s%s" % (name, ("（%s）" % err) if err else ""))
    try:
        import tkinter  # noqa: F401
        print("tkinter     : 可用")
    except Exception as exc:  # pragma: no cover
        print("tkinter     : 不可用（%s）" % exc)
    if not ok:
        print()
        print("如何解决：按上面的下载地址安装 ffmpeg，然后任选一种方式让本工具找到它：")
        print("  1) 把 bin\\ffmpeg.exe 放到：%s" % (ROOT / "bin" / "ffmpeg.exe"))
        print("  2) 或在 config.json 的 ffmpeg_path 填写 ffmpeg.exe 绝对路径；")
        print("  3) 或把 ffmpeg 所在目录加入系统 PATH。")
        print("完成后重新运行：python run.py --check")
        return 1
    return 0


def cmd_import_file(cfg, watchlist, path_str: str, group: str = "",
                    quality: str = "") -> int:
    """从 Excel/CSV 批量导入主播（去重）；供 --import-file 使用，也验证打包版包含导入模块。"""
    from src import import_util
    path = Path(path_str)
    if not path.exists():
        print("文件不存在: %s" % path)
        return 2
    try:
        records, skipped, sheet_info = import_util.records_from_file(str(path))
    except Exception as exc:
        print("读取失败: %s" % exc)
        return 2
    existing = set()
    for it in watchlist.all():
        key = import_util.target_key(it.web_rid or it.url or it.target)
        if key:
            existing.add(key)
    accepted, dup, invalid = import_util.dedupe(records, existing)
    usable = []
    name_only = 0
    for rec in accepted:
        try:
            key = import_util.target_key(rec.get("target") or "")
        except Exception:
            key = ""
        if key.startswith("txt:"):
            # 仅昵称条目：保留但强制停用（无法查询直播，不参与监控；导出再导入不丢数据）
            fixed = dict(rec)
            fixed["enabled"] = "否"
            usable.append(fixed)
            name_only += 1
            continue
        usable.append(rec)
    accepted = usable
    invalid += name_only
    if name_only:
        print("%d 条只有昵称、没有房间号/链接的条目已保留为「停用」状态（不参与监控）" % name_only)
    q = quality or cfg.get_str("quality")
    added = 0
    restored_disabled = 0
    import logging as _logging
    _store_log = _logging.getLogger("dyrec.storage")
    _old_level = _store_log.level
    _store_log.setLevel(_logging.WARNING)   # 批量导入时不逐条打印「添加主播」
    try:
        for rec in accepted:
            auto = import_util.parse_bool(rec.get("auto_record"), True)
            item_quality = import_util.parse_quality(rec.get("quality"), q)
            item = watchlist.add_from_text(
                rec["target"], name=rec.get("name") or "", auto_record=auto,
                quality=item_quality, output_template=rec.get("output_template") or "",
                save=False)
            if item is None:
                continue
            if rec.get("web_rid") and not item.web_rid:
                item.web_rid = rec["web_rid"]
            if rec.get("homepage") and not item.url:
                item.url = rec["homepage"]
            if rec.get("group"):
                item.group = rec["group"]
            elif group:
                item.group = group
            if "enabled" in rec:
                item.enabled = import_util.parse_bool(rec.get("enabled"), True)
            if rec.get("note"):
                item.note = rec["note"]
            if item.enabled is False:
                restored_disabled += 1
            added += 1
    finally:
        _store_log.setLevel(_old_level)
    watchlist.save()
    print("工作表: %s" % "、".join(
        "%s(%d行/%d个)" % (i.get("sheet", ""), i.get("rows", 0), i.get("records", 0))
        for i in sheet_info if int(i.get("rows") or 0) > 0) or "（空）")
    total_rows = sum(int(i.get("rows") or 0) for i in sheet_info)
    print("文件行数: %d（共 %d 个 sheet）  解析有效: %d  无法识别: %d"
          % (total_rows, len(sheet_info), len(records), skipped + invalid))
    if name_only:
        print("其中仅昵称（无房间号/主页链接）%d 条，已按「停用」保留" % name_only)
    print("与现有列表重复: %d  新增: %d  当前列表共: %d" % (dup, added, len(watchlist)))
    return 0


def _dispatch(args, cfg, watchlist) -> int:
    """根据命令行参数执行对应模式。"""
    if args.check:
        return env_check(cfg, watchlist)
    if args.history:
        from src.cli import cmd_history
        return cmd_history(cfg, watchlist, args.days)
    if args.export_diagnostics:
        from src.cli import cmd_export_diagnostics
        return cmd_export_diagnostics(cfg, watchlist, args.export_diagnostics, args.days)
    if args.list:
        from src.cli import cmd_list
        return cmd_list(watchlist)
    if getattr(args, "activity_report", None) is not None and args.activity_report != "":
        from src import activity
        try:
            rows = activity.collect_activity(
                watchlist, cfg.output_root, cfg.get_str("output_template"), backfill=True)
        except Exception as exc:
            print("统计失败: %s" % exc)
            return 2
        out = args.activity_report
        n = activity.write_csv(rows, out)
        never = sum(1 for r in rows if not r.get("last_live_at"))
        over7 = sum(1 for r in rows if r.get("last_live_at") and (r.get("days") or 0) > 7)
        over30 = sum(1 for r in rows if r.get("last_live_at") and (r.get("days") or 0) > 30)
        print("主播活跃度：共 %d 个 · 从未见开播 %d · 超 7 天未开播 %d · 超 30 天 %d"
              % (n, never, over7, over30))
        print("已导出: %s" % out)
        return 0
    if getattr(args, "convert_leftovers", False):
        from src import remux_legacy
        from src import ffmpeg_util
        try:
            root = cfg.output_root
        except Exception:
            print("无法确定输出目录")
            return 2
        files = remux_legacy.find_sources(root, min_age_seconds=120)
        if not files:
            print("输出目录中没有需要补转的文件：%s" % root)
            return 0
        ok, ffmpeg_path, info = ffmpeg_util.check_ffmpeg(cfg.get_str("ffmpeg_path"))
        if not ok:
            print("未找到可用的 ffmpeg：%s" % info)
            return 2
        print("发现 %d 个待补转文件，开始转换…" % len(files))
        keep = cfg.get_bool("keep_source")
        result = remux_legacy.convert_sources(
            files, ffmpeg_path, keep_source=keep,
            on_progress=lambda i, n, p, ok2, msg: print("  %d/%d %s %s"
                                                       % (i, n, p.name, "OK" if ok2 else "失败")))
        print("补转完成：成功 %d / 失败 %d / 共 %d；删除源文件 %d"
              % (result["converted"], result["failed"], result["total"],
                 result["deleted"]))
        return 0 if result["failed"] == 0 else 1
    if getattr(args, "cleanup", False):
        from src import remux_legacy
        from src import ffmpeg_util
        try:
            root = cfg.output_root
        except Exception:
            print("无法确定输出目录")
            return 2
        ok, ffmpeg_path, info = ffmpeg_util.check_ffmpeg(cfg.get_str("ffmpeg_path"))
        if not ok:
            print("未找到可用的 ffmpeg：%s" % info)
            return 2
        # 1) 补转残留 flv/ts
        files = remux_legacy.find_sources(root, min_age_seconds=120)
        conv = {"converted": 0, "failed": 0, "total": 0, "deleted": 0}
        if files:
            print("发现 %d 个残留 flv/ts，开始补转…" % len(files))
            conv = remux_legacy.convert_sources(
                files, ffmpeg_path, keep_source=cfg.get_bool("keep_source"),
                on_progress=lambda i, n, p, ok2, msg: print(
                    "  %d/%d %s %s" % (i, n, p.name, "OK" if ok2 else "失败")))
            print("补转完成：成功 %d / 失败 %d / 删除源文件 %d"
                  % (conv["converted"], conv["failed"], conv["deleted"]))
        else:
            print("没有残留 flv/ts")
        # 2) 删除时长低于 min_keep_seconds 的短视频
        min_keep = cfg.get_int("min_keep_seconds")
        if min_keep <= 0:
            print("min_keep_seconds=0（关闭），跳过短视频清理")
            return 0 if conv["failed"] == 0 else 1
        print("扫描时长低于 %d 秒的短视频（ffmpeg 解析，可能较慢）…" % min_keep)
        shorts = remux_legacy.find_short_videos(root, ffmpeg_path, min_keep,
                                                min_age_seconds=120)
        if not shorts:
            print("没有需要删除的短视频")
            return 0 if conv["failed"] == 0 else 1
        print("发现 %d 个短视频，开始删除…" % len(shorts))
        short_res = remux_legacy.cleanup_short_videos(shorts)
        print("短视频清理完成：删除 %d / 失败 %d" % (short_res["deleted"], short_res["failed"]))
        return 0 if conv["failed"] == 0 and short_res["failed"] == 0 else 1
    if getattr(args, "export_watchlist", ""):
        from src import export_util
        try:
            count = export_util.export_watchlist(watchlist, args.export_watchlist)
        except Exception as exc:
            print("导出失败: %s" % exc)
            return 2
        print("已导出 %d 个主播 -> %s" % (count, args.export_watchlist))
        return 0
    if getattr(args, "import_file", ""):
        return cmd_import_file(cfg, watchlist, args.import_file,
                               getattr(args, "import_group", ""),
                               getattr(args, "import_quality", ""))
    if args.add:
        from src.cli import cmd_add
        return cmd_add(cfg, watchlist, args.add, args.name)
    if args.remove:
        key = args.remove.strip()
        item = watchlist.get(key) or watchlist.find_by_room(key)
        if item is None:
            print("未找到主播: %s" % key)
            return 1
        watchlist.remove(item.id)
        print("已删除主播: %s" % item.display_name)
        return 0
    if args.once:
        from src.cli import cmd_once
        return cmd_once(cfg, watchlist)

    # 长驻模式（GUI / --cli）加单实例锁，避免重复启动导致重复录制
    from src.instance_guard import acquire_long_running_guard
    from src import history as _history
    guard, guard_msg = acquire_long_running_guard(_history.default_db_path().parent)
    if guard is None:
        print(guard_msg)
        return 3
    try:
        if args.cli:
            from src.cli import cmd_monitor
            return cmd_monitor(cfg, watchlist)
        # 默认启动 GUI
        from src.gui import run_gui
        run_gui(cfg, watchlist)
        return 0
    finally:
        guard.release()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    from src import __version__
    if args.version:
        print("抖音直播录制工具（本地自用版）v%s" % __version__)
        return 0

    from src.logger import setup_logging, setup_console
    setup_console()
    from src.config import load_config
    config_path = Path(args.config) if args.config else None
    cfg = load_config(config_path)
    if args.log_level:
        cfg["log_level"] = args.log_level.upper()
    if args.mock:
        cfg["use_mock_api"] = True
    from src.storage import Watchlist
    watchlist = Watchlist(Path(args.watchlist) if args.watchlist else None)
    # 日志：控制台 + 按天日志 logs/run-YYYY-MM-DD.log（保留 log_keep_days 天）
    setup_logging(cfg.log_dir_path, cfg.get_str("log_level"),
                  keep_days=cfg.get_int("log_keep_days"))

    from src import history
    mode = " ".join(str(a) for a in ((argv if argv is not None else sys.argv[1:]) or []))[:300]
    run_id = history.record_run_start(version=__version__, note=mode)
    exit_code = 0
    try:
        exit_code = _dispatch(args, cfg, watchlist)
    except Exception as exc:
        history.record_error("run.py", "运行异常: %s\n%s" % (exc, traceback.format_exc()))
        raise
    finally:
        history.record_run_end(run_id)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
